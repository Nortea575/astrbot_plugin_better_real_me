"""SQLite-backed state engine. All entry points share the same transactions."""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .groups import evaluate_group, normalize_group
from .indoor import (
    default_indoor,
    group_path,
    normalize_indoor,
    resolve_destination,
    room_path,
    shortest_path,
)
from .schedule import (
    active_entry,
    boundaries,
    default_schedule,
    normalize_schedule,
    schedule_runtime,
)
from .schema import (
    StateError,
    identifier,
    normalize,
    references,
    stage_for,
    validate_value,
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def actor_context(raw=None):
    raw = raw or {}
    return {
        key: str(raw.get(key) or default)
        for key, default in [
            ("character", "default"),
            ("conversation", "preview"),
            ("user", "preview"),
        ]
    }


def real_actor_identity(actor):
    """Return the platform, QQ account, and persona for a real QQ actor."""
    if not isinstance(actor, dict):
        return None
    character = str(actor.get("character") or "")
    platform, separator, remainder = character.partition(":")
    if not separator or not platform:
        return None
    qq, separator, persona = remainder.partition(":")
    if (
        not separator
        or not qq.isascii()
        or not qq.isdigit()
        or not qq.strip("0")
        or not persona
    ):
        return None
    return platform, qq, persona


def real_actor_context(raw):
    """Return a canonical registered-context shape only for a real QQ actor."""
    if not isinstance(raw, dict):
        return None
    if not all(key in raw for key in ("character", "conversation", "user")):
        return None
    actor = {
        key: str(raw.get(key) or "").strip()
        for key in ("character", "conversation", "user")
    }
    if not all(actor.values()) or real_actor_identity(actor) is None:
        return None
    if actor["conversation"] == "preview" or actor["user"] == "preview":
        return None
    return actor


def matches(clause, values):
    left, right = values[clause["state"]], clause["value"]
    op = clause["op"]
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right


class Engine:
    def __init__(self, path, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.clock = clock
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime (
                state_id TEXT NOT NULL, scope_key TEXT NOT NULL, value TEXT NOT NULL,
                updated REAL NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(state_id, scope_key));
            CREATE TABLE IF NOT EXISTS contexts (key TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, platform_id TEXT NOT NULL, qq TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                UNIQUE(platform_id, qq));
            CREATE TABLE IF NOT EXISTS seen (token TEXT PRIMARY KEY, result TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS indoor_locations (
                character_key TEXT PRIMARY KEY, room_id TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS indoor_seen (
                character_key TEXT NOT NULL, token TEXT NOT NULL, target_id TEXT NOT NULL,
                result TEXT NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(character_key, token));
        """)
        self.db.commit()
        from .life import LifeRuntime

        self.life = LifeRuntime(self)
        with self.transaction():
            # Upgrade stored flat graphs once, without creating locations or
            # accounts when this is a new, empty environment.
            self._indoor()
            from . import life_presets

            migrate_installed = getattr(life_presets, "migrate_installed_preset", None)
            if migrate_installed:
                migrate_installed(self)
        self.life.migrate_legacy_preset_data()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def _account_identity(platform_id, qq):
        platform_id = str(platform_id or "").strip()
        qq = str(qq or "").strip()
        if (
            not platform_id
            or ":" in platform_id
            or any(character.isspace() for character in platform_id)
            or not qq.isascii()
            or not qq.isdigit()
            or not qq.strip("0")
        ):
            raise StateError("QQ账号必须来自有效的平台标识和真实数字QQ号")
        return platform_id, qq

    def _record_account(self, platform_id, qq, name=""):
        platform_id, qq = self._account_identity(platform_id, qq)
        name = name.strip() if isinstance(name, str) else ""
        account_id = f"{platform_id}:{qq}"
        self.db.execute(
            "INSERT INTO accounts(id,platform_id,qq,name,updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(platform_id,qq) DO UPDATE SET "
            "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE accounts.name END, "
            "updated=excluded.updated",
            (account_id, platform_id, qq, name, self.clock()),
        )
        row = self.db.execute(
            "SELECT id,platform_id,qq,name FROM accounts WHERE platform_id=? AND qq=?",
            (platform_id, qq),
        ).fetchone()
        return dict(row)

    def record_account(self, platform_id, qq, name=""):
        """Record an independently observed QQ bot account without a context.

        Args:
            platform_id: AstrBot platform identifier.
            qq: The account's real numeric QQ identifier.
            name: Optional account display name; blank values preserve stored names.

        Returns:
            The de-duplicated account record.

        Raises:
            StateError: If the platform or QQ identifier is invalid.
        """
        with self.transaction():
            return self._record_account(platform_id, qq, name)

    def _config(self, key):
        row = self.db.execute("SELECT body FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else []

    def _save_config(self, key, value):
        self.db.execute(
            "INSERT INTO config VALUES (?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
            (key, dump(value)),
        )

    def _groups(self, definitions=None):
        """Load and validate all groups without creating independent runtime rows.

        Args:
            definitions: Optional base state definitions to validate against.

        Returns:
            Normalized group definitions in their configured order.

        Raises:
            StateError: If a group has invalid factors, scope, category, or ID.
        """
        definitions = self._config("states") if definitions is None else definitions
        indexed = {state["id"]: state for state in definitions}
        normalized = []
        group_ids = set()
        categories = {item["id"] for item in self._config("categories")} | {""}
        raw_groups = self._config("groups")
        if not isinstance(raw_groups, list):
            raise StateError("综合指标配置格式错误")
        for raw in raw_groups:
            group = normalize_group(raw, indexed)
            if group["id"] in indexed or group["id"] in group_ids:
                raise StateError("综合指标标识与现有状态或指标冲突")
            if group["category"] not in categories:
                raise StateError("综合指标所选分类不存在")
            group_ids.add(group["id"])
            normalized.append(group)
        return normalized

    def groups(self):
        """Return all normalized composite group definitions."""
        with self.lock:
            return self._groups()

    def _indoor(self):
        """Load the normalized indoor configuration and upgrade flat graphs."""
        row = self.db.execute("SELECT body FROM config WHERE key='indoor'").fetchone()
        if not row:
            return default_indoor()
        raw = json.loads(row[0])
        indoor = normalize_indoor(raw)
        if raw.get("schema_version") != 2 or not isinstance(raw.get("groups"), list):
            self._save_config("indoor", indoor)
        return indoor

    def save_indoor(self, raw, relocate=None):
        """Save the shared room graph and occupied-room relocations atomically.

        Args:
            raw: The complete new room graph configuration.
            relocate: Optional mapping from each deleted room to a room in the
                new graph. An empty target clears positions only when the new
                graph contains no rooms.

        Returns:
            The normalized configuration saved to the database.

        Raises:
            StateError: If the graph is invalid, an occupied room lacks a
                relocation, or any relocation target is invalid. The
                transaction rolls back all position and configuration writes.
        """
        relocate = {} if relocate is None else relocate
        if not isinstance(relocate, dict):
            raise StateError("房间迁移配置必须为对象")

        with self.transaction():
            self._settle_enabled_lives(self.clock())
            old = self._indoor()
            normalized = normalize_indoor(raw)
            old_ids = {room["id"] for room in old["rooms"]}
            new_ids = {room["id"] for room in normalized["rooms"]}
            removed_ids = old_ids - new_ids
            life_room_refs = {
                step.get("room_id")
                for action in self._config("life_actions")
                for step in action.get("steps", [])
                if step.get("room_id")
            }
            removed_life_rooms = removed_ids & life_room_refs
            if removed_life_rooms:
                raise StateError(
                    "生活行动仍引用房间：" + "、".join(sorted(removed_life_rooms))
                )
            room_by_id = {room["id"]: room for room in normalized["rooms"]}

            checked_relocations = {}
            for old_id, target_id in relocate.items():
                old_id = identifier(old_id, "被删除房间标识")
                if old_id not in removed_ids:
                    raise StateError(f"迁移来源不是本次删除的房间：{old_id}")
                if target_id == "":
                    if normalized["rooms"]:
                        raise StateError("仅清空最后房间时可将位置迁移目标设为空")
                else:
                    target_id = identifier(target_id, "迁移目标房间标识")
                    if target_id not in room_by_id:
                        raise StateError(f"迁移目标房间不存在：{target_id}")
                checked_relocations[old_id] = target_id

            for old_id in removed_ids:
                row = self.db.execute(
                    "SELECT COUNT(*) FROM indoor_locations WHERE room_id=?",
                    (old_id,),
                ).fetchone()
                occupied = row[0]
                if not occupied:
                    continue
                if old_id not in checked_relocations:
                    raise StateError(
                        f"房间“{old_id}”仍被{occupied}个角色占用，请指定迁移目标"
                    )
                target_id = checked_relocations[old_id]
                if target_id:
                    self.db.execute(
                        "UPDATE indoor_locations SET room_id=?,updated=? WHERE room_id=?",
                        (target_id, self.clock(), old_id),
                    )
                else:
                    self.db.execute(
                        "DELETE FROM indoor_locations WHERE room_id=?", (old_id,)
                    )

            self._save_config("indoor", normalized)
            return normalized

    @staticmethod
    def _room_map(indoor):
        return {room["id"]: room for room in indoor["rooms"]}

    def _effective_room_id(self, actor, indoor, *, initialize=False):
        """Resolve a character's stored room or its configured initial room.

        Args:
            actor: Normalized actor context; only its character key is used.
            indoor: Validated graph configuration.
            initialize: Persist the initial room when no position exists.

        Returns:
            The character's effective room identifier, or an empty string when
            no initial room is configured.

        Raises:
            StateError: If a stored position refers to a room missing from the
                validated graph.
        """
        actor = self._indoor_actor(actor)
        row = self.db.execute(
            "SELECT room_id FROM indoor_locations WHERE character_key=?",
            (actor["character"],),
        ).fetchone()
        if row:
            room_id = row[0]
            if room_id not in self._room_map(indoor):
                raise StateError(f"角色当前位置已失效：{room_id}")
            return room_id
        initial = indoor["initial_room_id"]
        if initialize and initial:
            self.db.execute(
                "INSERT OR IGNORE INTO indoor_locations VALUES (?,?,?)",
                (actor["character"], initial, self.clock()),
            )
        return initial

    def _indoor_actor(self, actor):
        identity = real_actor_identity(actor)
        if not identity:
            return actor
        account_id = f"{identity[0]}:{identity[1]}"
        return (
            self.life.canonical_actor(account_id)
            if self.life.is_configured(account_id)
            else actor
        )

    def _indoor_runtime(self, actor, indoor, *, character_ids=None):
        """Build current room, exits, and occupancy for one actor.

        Args:
            actor: Normalized actor context used for room and exit details.
            indoor: Validated room graph.
            character_ids: Optional allowlist for aggregate occupancy counts.

        Returns:
            The actor's current room, adjacent exits, and room occupancy map.
        """
        rooms = self._room_map(indoor)
        room_id = self._effective_room_id(actor, indoor, initialize=True)
        occupancy = {key: 0 for key in rooms}
        for row in self.db.execute(
            "SELECT character_key,room_id FROM indoor_locations"
        ):
            if character_ids is not None and row["character_key"] not in character_ids:
                continue
            if row["room_id"] in occupancy:
                occupancy[row["room_id"]] += 1

        exits = []
        if room_id:
            neighbors = set()
            for edge in indoor["connections"]:
                if edge["a"] == room_id:
                    neighbors.add(edge["b"])
                elif edge["b"] == room_id:
                    neighbors.add(edge["a"])
            exits = []
            for key in sorted(neighbors):
                destination = dict(rooms[key])
                destination["group_path"] = group_path(indoor, destination["group_id"])
                destination["location_path"] = room_path(indoor, key)
                exits.append(destination)
        room = dict(rooms[room_id]) if room_id else None
        group_id = room["group_id"] if room else ""
        return {
            "room_id": room_id,
            "room": room,
            "exits": exits,
            "occupancy": occupancy,
            "group_id": group_id,
            "location_path": room_path(indoor, room_id),
            "group_path": group_path(indoor, group_id) if group_id else [],
        }

    def _route(self, actor, indoor, target_room_id):
        """Build a route preview from the character's actual effective room.

        Args:
            actor: Normalized actor context.
            indoor: Validated graph configuration.
            target_room_id: Requested destination room identifier.

        Returns:
            Reachability, endpoints, the shortest path, and a user-facing
            result message.

        Raises:
            StateError: If the destination is invalid or there is no valid
                current or initial room from which to route.
        """
        target_room_id = identifier(target_room_id, "目标房间或分组标识")
        rooms = self._room_map(indoor)
        start = self._effective_room_id(actor, indoor)
        if not start or start not in rooms:
            raise StateError("尚未设置有效的初始房间")
        resolved_room_id = resolve_destination(indoor, start, target_room_id)
        if resolved_room_id is None:
            if target_room_id in {group["id"] for group in indoor["groups"]}:
                raise StateError(f"目标分组尚未设置入口房间：{target_room_id}")
            raise StateError(f"目标房间或分组不存在：{target_room_id}")
        path = shortest_path(indoor, start, resolved_room_id)
        target_group = next(
            (group for group in indoor["groups"] if group["id"] == target_room_id),
            None,
        )
        target_name = (
            " / ".join(item["name"] for item in group_path(indoor, target_group["id"]))
            if target_group
            else room_path(indoor, resolved_room_id)
        )
        return {
            "reachable": path is not None,
            "from_room_id": start,
            "to_room_id": resolved_room_id,
            "requested_target_id": target_room_id,
            "target_group_id": target_group["id"] if target_group else "",
            "path": path or [],
            "message": (
                f"可从“{room_path(indoor, start)}”到达“{target_name}”。"
                if path is not None
                else f"从“{room_path(indoor, start)}”无法到达“{target_name}”。"
            ),
        }

    def route_indoor(self, actor, target_room_id):
        """Return a read-only indoor route preview for a character."""
        actor = actor_context(actor)
        with self.transaction():
            return self._route(actor, self._indoor(), target_room_id)

    def look_indoor(self, actor, group_id=""):
        """Return current room perception and only one selected group level."""
        actor = actor_context(actor)
        with self.transaction():
            self._advance(self._config("states"), actor)
            actor = self._indoor_actor(actor)
            indoor = self._indoor()
            runtime = self._indoor_runtime(actor, indoor)
            if not group_id:
                group_id = runtime["group_id"] or indoor["root_group_id"]
            else:
                group_id = identifier(group_id, "查询分组标识")
            group_by_id = {group["id"]: group for group in indoor["groups"]}
            selected_group = group_by_id.get(group_id)
            if selected_group is None:
                raise StateError(f"查询分组不存在：{group_id}")
            direct_groups = [
                group for group in indoor["groups"] if group["parent_id"] == group_id
            ]
            child_group_ids = {group["id"] for group in direct_groups}
            direct_rooms = [
                room for room in indoor["rooms"] if room["group_id"] == group_id
            ]
            local_node_by_room = {}
            for room in indoor["rooms"]:
                if room["group_id"] == group_id:
                    local_node_by_room[room["id"]] = room["id"]
                    continue
                path = group_path(indoor, room["group_id"])
                branch = next(
                    (item["id"] for item in path if item["id"] in child_group_ids),
                    "",
                )
                if branch:
                    local_node_by_room[room["id"]] = branch
            aggregate_connections = []
            for edge in indoor["connections"]:
                left_node = local_node_by_room.get(edge["a"])
                right_node = local_node_by_room.get(edge["b"])
                if not left_node or not right_node or left_node == right_node:
                    continue
                key = (left_node, right_node, edge["a"], edge["b"])
                if key[1] < key[0]:
                    key = (key[1], key[0], edge["b"], edge["a"])
                aggregate_connections.append(
                    {
                        "a": key[0],
                        "b": key[1],
                        "a_room_id": key[2],
                        "b_room_id": key[3],
                    }
                )
            aggregate_connections.sort(
                key=lambda edge: (
                    edge["a"],
                    edge["b"],
                    edge["a_room_id"],
                    edge["b_room_id"],
                )
            )
            parent_group = group_by_id.get(selected_group["parent_id"])
            return {
                "room": runtime["room"],
                "exits": runtime["exits"],
                "location_path": runtime["location_path"],
                "group": selected_group,
                "parent_group": parent_group,
                "groups": direct_groups,
                "rooms": direct_rooms,
                "connections": aggregate_connections,
            }

    def move_indoor(self, actor, target_room_id, token=""):
        """Resolve and persist a move from the current room under lock.

        Args:
            actor: Normalized actor context. The character key scopes location.
            target_room_id: Requested destination room identifier.
            token: Optional per-request idempotency token supplied by the view.

        Returns:
            A mapping containing the current full snapshot and the move result.
            A duplicate token returns its original result with a fresh snapshot.

        Raises:
            StateError: If the destination is invalid, unreachable, the token
                is reused for another destination, or no initial room exists.
        """
        actor = actor_context(actor)
        target_room_id = identifier(target_room_id, "目标房间或分组标识")
        token = str(token or "").strip()
        if len(token) > 256:
            raise StateError("移动请求标识过长")

        with self.transaction():
            definitions = self._config("states")
            self._advance(definitions, actor)
            actor = self._indoor_actor(actor)
            life_account_id = self.life.account_for_actor(actor)
            if life_account_id and self.life.is_enabled(life_account_id):
                # A life actor has no chat context to trigger _advance. Settle
                # elapsed activity and travel directly against the QQ body.
                self.life._settle_account(
                    life_account_id, self.clock(), deterministic=True
                )
            indoor = self._indoor()
            rooms = self._room_map(indoor)
            if token:
                previous = self.db.execute(
                    "SELECT target_id,result FROM indoor_seen WHERE character_key=? AND token=?",
                    (actor["character"], token),
                ).fetchone()
                if previous:
                    if previous["target_id"] != target_room_id:
                        raise StateError("移动请求标识已用于其他目标")
                    move = json.loads(previous["result"])
                    move["duplicate"] = True
                    snapshot = self._snapshot(definitions, actor)
                    return {"snapshot": snapshot, "move": move}

            result = self._route(actor, indoor, target_room_id)
            if not result["reachable"]:
                raise StateError(result["message"])
            from_id = result["from_room_id"]
            interrupted = None
            destination_room_id = result["to_room_id"]
            if from_id != destination_room_id:
                if life_account_id and self.life.is_enabled(life_account_id):
                    interrupted = self.life.interrupt_for_move(
                        life_account_id, destination_room_id, self.clock()
                    )
                self.db.execute(
                    "INSERT INTO indoor_locations VALUES (?,?,?) "
                    "ON CONFLICT(character_key) DO UPDATE SET "
                    "room_id=excluded.room_id,updated=excluded.updated",
                    (actor["character"], destination_room_id, self.clock()),
                )
            elif not self.db.execute(
                "SELECT 1 FROM indoor_locations WHERE character_key=?",
                (actor["character"],),
            ).fetchone():
                self.db.execute(
                    "INSERT INTO indoor_locations VALUES (?,?,?)",
                    (actor["character"], destination_room_id, self.clock()),
                )

            target_group = next(
                (group for group in indoor["groups"] if group["id"] == target_room_id),
                None,
            )
            destination_name = rooms[destination_room_id]["name"]
            if target_group and destination_room_id == from_id:
                destination_name = target_group["name"]
            move = {
                "ok": True,
                "from_room_id": from_id,
                "to_room_id": destination_room_id,
                "path": result["path"],
                "message": (
                    f"已在“{destination_name}”，位置不变。"
                    if from_id == destination_room_id
                    else f"已从“{room_path(indoor, from_id)}”到达“{destination_name}”。"
                    + (
                        f"已中止正在进行的“{interrupted['activity_name']}”。"
                        if interrupted
                        else ""
                    )
                ),
                "duplicate": False,
                "activity_interrupted": bool(interrupted),
                "interrupted_activity_name": interrupted["activity_name"]
                if interrupted
                else "",
                "requested_target_id": target_room_id,
            }
            if token:
                self.db.execute(
                    "INSERT INTO indoor_seen VALUES (?,?,?,?,?)",
                    (
                        actor["character"],
                        token,
                        target_room_id,
                        dump(move),
                        self.clock(),
                    ),
                )
                self.db.execute(
                    "DELETE FROM indoor_seen WHERE rowid NOT IN "
                    "(SELECT rowid FROM indoor_seen ORDER BY updated DESC LIMIT 1000)"
                )
            snapshot = self._snapshot(definitions, actor)
            return {"snapshot": snapshot, "move": move}

    def scope_key(self, definition, actor):
        if (
            definition["id"]
            in self.life.owned_state_ids() | self.life.owned_group_ids()
        ):
            identity = real_actor_identity(actor)
            if identity:
                return dump([f"life:{identity[0]}:{identity[1]}"])
            character = str(actor.get("character") or "")
            if character.startswith("life:"):
                return dump([character])
        parts = [actor["character"]]
        if definition["scope"] != "character":
            parts.append(
                actor["conversation"]
                if definition["scope"] == "conversation"
                else actor["user"]
            )
        return dump(parts)

    def _row(self, definition, actor):
        key = self.scope_key(definition, actor)
        row = self.db.execute(
            "SELECT * FROM runtime WHERE state_id=? AND scope_key=?",
            (definition["id"], key),
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO runtime VALUES (?,?,?,?,?)",
                (
                    definition["id"],
                    key,
                    dump(definition["initial"]),
                    self.clock(),
                    "初始化",
                ),
            )
            row = self.db.execute(
                "SELECT * FROM runtime WHERE state_id=? AND scope_key=?",
                (definition["id"], key),
            ).fetchone()
        return dict(row)

    def _write_value(self, definition, actor, value, reason):
        self._write_value_at(definition, actor, value, reason, self.clock())
        if definition["id"] in self.life.owned_state_ids():
            account_id = self.life.account_for_actor(actor)
            if account_id:
                self.db.execute(
                    "UPDATE life_accounts SET revision=revision+1 WHERE account_id=?",
                    (account_id,),
                )

    def _write_value_at(self, definition, actor, value, reason, timestamp):
        self.db.execute(
            "UPDATE runtime SET value=?,updated=?,reason=? WHERE state_id=? AND scope_key=?",
            (
                dump(value),
                timestamp,
                reason,
                definition["id"],
                self.scope_key(definition, actor),
            ),
        )

    def _settle_enabled_lives(self, now=None):
        now = self.clock() if now is None else float(now)
        for row in self.db.execute(
            "SELECT account_id FROM life_accounts WHERE enabled=1"
        ):
            self.life._settle_account(row["account_id"], now)

    def save_state(self, raw, creating=False):
        with self.transaction():
            self._settle_enabled_lives(self.clock())
            if not isinstance(raw, dict):
                raise StateError("状态定义必须为对象")
            definitions = self._config("states")
            groups = self._groups(definitions)
            if raw.get("id") in {group["id"] for group in groups}:
                raise StateError("状态标识与综合指标冲突")
            current = next((s for s in definitions if s["id"] == raw.get("id")), None)
            if creating and current:
                raise StateError("状态标识已经存在，请使用其他标识")
            if not creating and not current:
                raise StateError("状态不存在")
            if current:
                # Settle elapsed time with the old rules before changing rates or events.
                for row in list(self.db.execute("SELECT body FROM contexts")):
                    self._advance(definitions, json.loads(row[0]))
            normalized = normalize(raw, definitions)
            if normalized["category"] and normalized["category"] not in {
                c["id"] for c in self._config("categories")
            }:
                raise StateError("所选分类不存在")
            if current:
                if any(current[k] != normalized[k] for k in ["type", "scope"]):
                    raise StateError("已有状态的类型和归属不可直接修改；请新建状态")
                for row in self.db.execute(
                    "SELECT value FROM runtime WHERE state_id=?", (current["id"],)
                ):
                    validate_value(normalized, json.loads(row[0]))
                definitions = [
                    normalized if s["id"] == normalized["id"] else s
                    for s in definitions
                ]
            else:
                definitions.append(normalized)
            # Revalidate incoming references when a range, option, or rule changes.
            definitions = [normalize(s, definitions) for s in definitions]
            self._groups(definitions)
            if current and (
                current["enabled"] != normalized["enabled"]
                or current["rate_per_hour"] != normalized["rate_per_hour"]
            ):
                self.db.execute(
                    "UPDATE runtime SET updated=? WHERE state_id=?",
                    (self.clock(), current["id"]),
                )
            self._save_config("states", definitions)
            return normalized

    def save_group(self, raw, creating=False):
        """Create or update one read-only composite state group.

        Args:
            raw: Complete group definition received from the editor.
            creating: Whether the operation must add a new group.

        Returns:
            The normalized definition saved to the configuration table.

        Raises:
            StateError: If the identifier, category, scope, or source factors
                conflict with the current configuration.
        """
        with self.transaction():
            if not isinstance(raw, dict):
                raise StateError("综合指标定义必须为对象")
            definitions = self._config("states")
            groups = self._groups(definitions)
            group_id = raw.get("id")
            current = next((group for group in groups if group["id"] == group_id), None)
            if group_id in {state["id"] for state in definitions}:
                raise StateError("综合指标标识与基础状态冲突")
            if creating and current:
                raise StateError("综合指标标识已经存在，请使用其他标识")
            if not creating and current is None:
                raise StateError("综合指标不存在")

            normalized = normalize_group(
                raw, {state["id"]: state for state in definitions}
            )
            categories = {item["id"] for item in self._config("categories")} | {""}
            if normalized["category"] not in categories:
                raise StateError("所选分类不存在")
            if current:
                groups = [
                    normalized if group["id"] == normalized["id"] else group
                    for group in groups
                ]
            else:
                groups.append(normalized)
            if len({group["id"] for group in groups}) != len(groups):
                raise StateError("综合指标标识不能重复")
            self._save_config("groups", groups)
            return normalized

    def delete_group(self, group_id):
        """Delete a composite definition without touching base state runtime."""
        with self.transaction():
            groups = self._groups()
            if not any(group["id"] == group_id for group in groups):
                raise StateError("综合指标不存在")
            self._save_config(
                "groups", [group for group in groups if group["id"] != group_id]
            )

    def _schedule(self, definitions=None):
        definitions = self._config("states") if definitions is None else definitions
        row = self.db.execute("SELECT body FROM config WHERE key='schedule'").fetchone()
        if row is None:
            return default_schedule()
        return normalize_schedule(json.loads(row[0]), definitions)

    def save_schedule(self, raw, category_changes=None):
        with self.transaction():
            definitions = self._config("states")
            normalized = normalize_schedule(raw, definitions)
            if category_changes is not None:
                from .schedule import apply_category_changes

                actions = self._config("life_actions")
                updated_actions = apply_category_changes(actions, category_changes)
                if updated_actions != actions:
                    self._save_config("life_actions", updated_actions)
            self._settle_enabled_lives(self.clock())
            old_schedule = self._schedule(definitions)
            for row in list(self.db.execute("SELECT body FROM contexts")):
                self._advance(definitions, json.loads(row[0]), schedule=old_schedule)
            self._save_config("schedule", normalized)
            return normalized

    def delete_state(self, state_id):
        with self.transaction():
            if state_id in self.life.owned_state_ids():
                raise StateError("生活内核状态仍由生活行动使用，请先移除其行动引用")
            self._settle_enabled_lives(self.clock())
            definitions = self._config("states")
            if not any(s["id"] == state_id for s in definitions):
                raise StateError("状态不存在")
            for group in self._groups(definitions):
                if any(factor["state_id"] == state_id for factor in group["factors"]):
                    raise StateError(
                        f"综合指标“{group['name']}”仍在引用此状态，请先移除该来源"
                    )
            for entry in self._schedule(definitions)["entries"]:
                if state_id in entry["rates"]:
                    raise StateError(
                        f"日程“{entry['name']}”仍在引用此状态，请先移除该日程的变化量"
                    )
            for other in definitions:
                if other["id"] != state_id and state_id in references(other):
                    raise StateError(f"{other['name']}仍在引用此状态，请先移除相关规则")
            self._save_config("states", [s for s in definitions if s["id"] != state_id])
            self.db.execute("DELETE FROM runtime WHERE state_id=?", (state_id,))
            # Old deduplication results must not affect a subsequently recreated state.
            self.db.execute("DELETE FROM seen")

    def category(self, action, category_id="", name=""):
        with self.transaction():
            categories = self._config("categories")
            if action in {"create", "rename"}:
                name = str(name).strip()
                if (
                    not name
                    or name == "未分类"
                    or any(
                        c["name"] == name and c["id"] != category_id for c in categories
                    )
                ):
                    raise StateError("分类名称不能为空、重复或使用保留名称“未分类”")
            if action == "create":
                categories.append({"id": uuid.uuid4().hex[:12], "name": name})
            else:
                if not category_id or category_id not in {c["id"] for c in categories}:
                    raise StateError("分类不存在，未分类不能重命名或删除")
                if action == "rename":
                    categories = [
                        {**c, "name": name} if c["id"] == category_id else c
                        for c in categories
                    ]
                elif action == "delete":
                    categories = [c for c in categories if c["id"] != category_id]
                    states = self._config("states")
                    groups = self._groups(states)
                    for state in states:
                        if state["category"] == category_id:
                            state["category"] = ""
                    for group in groups:
                        if group["category"] == category_id:
                            group["category"] = ""
                    self._save_config("states", states)
                    self._save_config("groups", groups)
                else:
                    raise StateError("不支持的分类操作")
            self._save_config("categories", categories)
            return categories

    def reorder(self, state_ids, assignments):
        with self.transaction():
            states = {s["id"]: s for s in self._config("states")}
            if (
                not isinstance(state_ids, list)
                or len(state_ids) != len(states)
                or set(state_ids) != set(states)
            ):
                raise StateError("列表已变化，请刷新后重新排序")
            categories = {c["id"] for c in self._config("categories")} | {""}
            for state_id, category_id in assignments.items():
                if state_id not in states or category_id not in categories:
                    raise StateError("状态或分类不存在")
                states[state_id]["category"] = category_id
            self._save_config("states", [states[s] for s in state_ids])

    @staticmethod
    def _bounded(definition, value):
        if definition["min"] is not None:
            value = max(definition["min"], value)
        if definition["max"] is not None:
            value = min(definition["max"], value)
        return value

    def _values(self, definitions, actor):
        return {s["id"]: json.loads(self._row(s, actor)["value"]) for s in definitions}

    def _effects(self, item, definitions, actor, values, reason, *, persist=True):
        indexed = {s["id"]: s for s in definitions}
        if not all(matches(c, values) for c in item["conditions"]):
            raise StateError("当前状态不满足执行条件")
        updated = dict(values)
        for effect in item["effects"]:
            target = indexed[effect["state"]]
            if not target["enabled"]:
                raise StateError(f"{target['name']}已停用")
            value = effect["value"]
            if effect["op"] == "add":
                value += updated[target["id"]]
                # Positive replenishment saturates; consumption must be affordable.
                if effect["value"] > 0 and target["max"] is not None:
                    value = min(value, target["max"])
            updated[target["id"]] = validate_value(target, value)
        for state_id, value in updated.items():
            if persist and values[state_id] != value:
                self._write_value(indexed[state_id], actor, value, reason)
        return updated

    def _thresholds(
        self,
        definitions,
        actor,
        before,
        after,
        *,
        eligible_ids=None,
        available_ids=None,
        persist=True,
        return_reasons=False,
    ):
        # A finite pass: effects of threshold events do not recursively emit new events.
        result = dict(after)
        reasons = {}
        for state in definitions:
            if eligible_ids is not None and state["id"] not in eligible_ids:
                continue
            if not state["enabled"]:
                continue
            for event in state["events"]:
                if event["trigger"] != "threshold":
                    continue
                if available_ids is not None:
                    references = {event["when"]["state"]}
                    references.update(
                        condition["state"] for condition in event["conditions"]
                    )
                    references.update(effect["state"] for effect in event["effects"])
                    if not references.issubset(available_ids):
                        continue
                if not matches(event["when"], before) and matches(event["when"], after):
                    # A non-executable threshold event leaves the triggering change intact.
                    try:
                        event_reason = f"阈值事件：{event['name']}"
                        updated = self._effects(
                            event,
                            definitions,
                            actor,
                            result,
                            event_reason,
                            persist=persist,
                        )
                        for state_id, value in updated.items():
                            if result[state_id] != value:
                                reasons[state_id] = event_reason
                        result = updated
                    except StateError:
                        continue
        return (result, reasons) if return_reasons else result

    def _advance(self, definitions, actor, *, schedule=None):
        now = self.clock()
        character = str(actor.get("character") or "")
        if character.startswith("life:"):
            account_id = character[len("life:") :]
            if self.life.is_enabled(account_id):
                values = {}
                for state in definitions:
                    if state["id"] in self.life.owned_state_ids():
                        row = self._row(state, actor)
                        values[state["id"]] = json.loads(row["value"])
                return values
            return {}

        identity = real_actor_identity(actor)
        life_account_id = f"{identity[0]}:{identity[1]}" if identity else ""
        life_ids = self.life.owned_state_ids()
        life_enabled = bool(life_account_id and self.life.is_enabled(life_account_id))
        if life_enabled:
            self.life._settle_account(life_account_id, now)
        self.db.execute(
            "INSERT INTO contexts VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET updated=excluded.updated",
            (dump(actor), dump(actor), now),
        )
        schedule = self._schedule(definitions) if schedule is None else schedule
        values, updated_at, reasons, available_ids = {}, {}, {}, set()
        for state in definitions:
            if state["id"] in life_ids:
                if life_enabled:
                    row = self._row(state, actor)
                    values[state["id"]] = json.loads(row["value"])
                    available_ids.add(state["id"])
                continue
            scope_key = self.scope_key(state, actor)
            exists = self.db.execute(
                "SELECT 1 FROM runtime WHERE state_id=? AND scope_key=?",
                (state["id"], scope_key),
            ).fetchone()
            row = self._row(state, actor)
            state_id = state["id"]
            if exists:
                available_ids.add(state_id)
            values[state_id] = json.loads(row["value"])
            updated_at[state_id] = row["updated"]
            reasons[state_id] = row["reason"]

        if updated_at:
            first_update = min(min(updated_at.values()), now)
            points = {first_update, now}
            points.update(
                timestamp
                for timestamp in updated_at.values()
                if first_update < timestamp < now
            )
            points.update(boundaries(schedule, first_update, now))
            timeline = sorted(points)
            indexed = {state["id"]: state for state in definitions}
            for start, end in itertools.pairwise(timeline):
                if end <= start:
                    continue
                eligible = {
                    state_id
                    for state_id, timestamp in updated_at.items()
                    if timestamp <= start
                }
                midpoint = (start + end) / 2
                entry = active_entry(schedule, midpoint)
                elapsed_hours = (end - start) / 3600
                before = dict(values)
                after = dict(values)
                for state_id in eligible:
                    state = indexed[state_id]
                    if not state["enabled"] or state["type"] != "number":
                        continue
                    rate = (
                        entry["rates"].get(state_id, state["rate_per_hour"])
                        if entry
                        else state["rate_per_hour"]
                    )
                    if rate:
                        after[state_id] = self._bounded(
                            state, before[state_id] + elapsed_hours * rate
                        )
                for state_id in eligible:
                    if after[state_id] != before[state_id]:
                        reasons[state_id] = "时间经过"
                after, event_reasons = self._thresholds(
                    definitions,
                    actor,
                    before,
                    after,
                    eligible_ids=eligible,
                    available_ids=available_ids,
                    persist=False,
                    return_reasons=True,
                )
                reasons.update(event_reasons)
                values = after
                for state_id in life_ids:
                    if state_id in values and values[state_id] != before[state_id]:
                        self._write_value_at(
                            next(
                                item for item in definitions if item["id"] == state_id
                            ),
                            actor,
                            values[state_id],
                            event_reasons.get(state_id, "生活状态变化"),
                            end,
                        )
                        self.db.execute(
                            "UPDATE life_accounts SET revision=revision+1 WHERE account_id=?",
                            (life_account_id,),
                        )

        for state in definitions:
            state_id = state["id"]
            if state_id in life_ids or state_id not in values:
                continue
            self._write_value(state, actor, values[state_id], reasons[state_id])
        return values

    def _snapshot(self, definitions, actor, *, include_paused_life=False):
        life_ids = self.life.owned_state_ids()
        life_group_ids = self.life.owned_group_ids()
        character = str(actor.get("character") or "")
        life_actor = character.startswith("life:")
        account_id = (
            character[len("life:") :]
            if life_actor
            else self.life.account_for_actor(actor)
        )
        life_enabled = bool(account_id and self.life.is_enabled(account_id))
        life_visible = bool(
            life_enabled
            or (
                include_paused_life
                and account_id
                and self.life.is_configured(account_id)
            )
        )
        if life_actor:
            visible_definitions = [
                item for item in definitions if item["id"] in life_ids
            ]
        elif life_visible:
            visible_definitions = definitions
        else:
            visible_definitions = [
                item for item in definitions if item["id"] not in life_ids
            ]
        all_groups = self._groups(definitions)
        if life_actor:
            visible_groups = [
                group for group in all_groups if group["id"] in life_group_ids
            ]
        elif life_visible:
            visible_groups = all_groups
        else:
            visible_groups = [
                group
                for group in all_groups
                if not any(
                    factor["state_id"] in life_ids for factor in group["factors"]
                )
            ]
        states = []
        for definition in visible_definitions:
            row = self._row(definition, actor)
            value = json.loads(row["value"])
            stage = stage_for(definition, value)
            scope_key = self.scope_key(definition, actor)
            states.append(
                {
                    "key": dump([definition["id"], scope_key]),
                    "scope_key": scope_key,
                    "context": actor,
                    "unbound": False,
                    "id": definition["id"],
                    "value": value,
                    "stage": stage["name"],
                    "stage_description": stage["description"],
                    "updated": row["updated"],
                    "reason": row["reason"],
                    "kind": "state",
                    "computed": False,
                }
            )
        states.extend(
            self._group_state_row(group, definitions, actor) for group in visible_groups
        )
        contexts = [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT body FROM contexts ORDER BY updated DESC LIMIT 30"
            )
        ]
        schedule = self._schedule(definitions)
        indoor = self._indoor()
        room_actor = self._indoor_actor(actor)
        if life_actor:
            room_actor = actor
        if life_actor or life_visible:
            for row in states:
                if row["id"] in life_ids or row["id"] in life_group_ids:
                    row["account_id"] = account_id
                    row["context"] = None
                    row["unbound"] = False
        return {
            "definitions": visible_definitions,
            "groups": visible_groups,
            "states": states,
            "categories": self._config("categories"),
            "context": None if life_actor else actor,
            "account_id": account_id
            if life_actor
            else (account_id if life_visible else ""),
            "contexts": contexts,
            "schedule": schedule,
            "schedule_runtime": schedule_runtime(schedule, self.clock()),
            "indoor": indoor,
            "indoor_runtime": self._indoor_runtime(room_actor, indoor),
        }

    def _group_state_row(self, group, definitions, actor=None, *, unbound=False):
        """Compute one scoped group row without writing a group runtime value.

        Args:
            group: Normalized composite group definition.
            definitions: Normalized base states indexed at computation time.
            actor: Real or in-memory context used for source values.
            unbound: Whether to use source initial values without a context.

        Returns:
            A state-shaped row with the calculated score and component details.
        """
        values = {}
        source_rows = []
        indexed = {state["id"]: state for state in definitions}
        for factor in group["factors"]:
            source = indexed[factor["state_id"]]
            if unbound:
                values[source["id"]] = source["initial"]
                source_rows.append(None)
            else:
                row = self._row(source, actor)
                values[source["id"]] = json.loads(row["value"])
                source_rows.append(row)
        value, components = evaluate_group(group, indexed, values)
        stage = stage_for(group, value)
        scope_key = "" if unbound else self.scope_key(group, actor)
        if unbound:
            updated, reason = None, "初始化"
        else:
            updated = max(row["updated"] for row in source_rows)
            reasons = {row["reason"] for row in source_rows}
            reason = next(iter(reasons)) if len(reasons) == 1 else "来源综合"
        return {
            "key": dump([group["id"], scope_key or "unbound"]),
            "scope_key": scope_key,
            "context": None if unbound else actor,
            "unbound": unbound,
            "id": group["id"],
            "value": value,
            "stage": stage["name"],
            "stage_description": stage["description"],
            "updated": updated,
            "reason": reason,
            "kind": "group",
            "computed": True,
            "components": components,
        }

    def snapshot(self, actor=None, *, include_paused_life=False):
        actor = actor_context(actor)
        if not isinstance(include_paused_life, bool):
            raise StateError("include_paused_life必须为布尔值")
        with self.transaction():
            definitions = self._config("states")
            self._advance(definitions, actor)
            return self._snapshot(
                definitions, actor, include_paused_life=include_paused_life
            )

    def registered_context(self, raw):
        """Return a real QQ context only when a real event already registered it.

        Args:
            raw: The client-supplied full context mapping.

        Returns:
            The canonical context previously recorded from a message event.

        Raises:
            StateError: If the context is malformed, synthetic, or unknown.
        """
        actor = real_actor_context(raw)
        if actor is None:
            raise StateError("请选择已由真实 QQ 消息登记的角色")
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM contexts WHERE key=?", (dump(actor),)
            ).fetchone()
        if row is None:
            raise StateError("该角色尚未通过真实 QQ 消息登记")
        try:
            stored = real_actor_context(json.loads(row["body"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = None
        if stored != actor:
            raise StateError("登记的角色上下文无效，请刷新后重试")
        return actor

    def overview(self, selected_actor=None):
        """Settle and return a stable aggregate view for every real QQ actor.

        Args:
            selected_actor: Optional real context to use for room details when
                it has already been registered by a message event.

        Returns:
            Shared configuration, all known real contexts and actors, every
            unique scoped state row, and the selected actor's indoor runtime.

        Notes:
            Synthetic or malformed historical contexts are ignored. When no
            real actor exists, state definitions are represented by unbound
            initial rows without creating runtime, context, or location data.
        """
        with self.transaction():
            definitions = self._config("states")
            groups = self._groups(definitions)
            for life_row in self.db.execute(
                "SELECT account_id FROM life_accounts WHERE enabled=1 ORDER BY account_id"
            ):
                self.life._settle_account(life_row["account_id"], self.clock())
            life_data = self.life.get_overview()
            life_accounts = {item["id"]: item for item in life_data["accounts"]}
            life_ids = self.life.owned_state_ids()
            life_group_ids = self.life.owned_group_ids()
            contexts = []
            for row in self.db.execute("SELECT body FROM contexts"):
                try:
                    actor = real_actor_context(json.loads(row["body"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    actor = None
                if actor is not None:
                    contexts.append(actor)
                    identity = real_actor_identity(actor)
                    self._record_account(identity[0], identity[1])
            contexts.sort(
                key=lambda actor: (
                    actor["character"],
                    actor["conversation"],
                    actor["user"],
                )
            )

            known = {dump(actor) for actor in contexts}
            preferred = real_actor_context(selected_actor)
            selected = (
                preferred
                if preferred is not None and dump(preferred) in known
                else None
            )
            if selected is None and contexts:
                selected = contexts[0]
            preferred_life_account = ""
            if isinstance(selected_actor, dict):
                selected_character = str(selected_actor.get("character") or "")
                if selected_character.startswith("life:"):
                    candidate = selected_character[len("life:") :]
                    if self.life.is_enabled(candidate):
                        preferred_life_account = candidate
            selected_account_id = preferred_life_account
            if not selected_account_id and selected is not None:
                selected_identity = real_actor_identity(selected)
                if selected_identity:
                    candidate = f"{selected_identity[0]}:{selected_identity[1]}"
                    if self.life.is_enabled(candidate):
                        selected_account_id = candidate
            if not selected_account_id and selected is None:
                selected_account_id = next(
                    (item["id"] for item in life_data["accounts"] if item["enabled"]),
                    "",
                )

            if contexts:
                # Iterate in a stable order so the representative context for a
                # shared scope does not change as recency timestamps are updated.
                for actor in contexts:
                    self._advance(definitions, actor)

            actors_by_character = {}
            for actor in contexts:
                actors_by_character.setdefault(actor["character"], actor)
            actors = []
            for character, actor in sorted(actors_by_character.items()):
                identity = real_actor_identity(actor)
                if identity is None:
                    continue
                account_id = f"{identity[0]}:{identity[1]}"
                if self.life.is_enabled(account_id):
                    continue
                _, qq, persona = identity
                actors.append(
                    {
                        "character": character,
                        "actor_id": character,
                        "account_id": account_id,
                        "qq": qq,
                        "name": persona,
                        "avatar_url": f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100",
                        "context": actor,
                    }
                )

            for account_id, life_account in sorted(life_accounts.items()):
                if not life_account["enabled"]:
                    continue
                life_actor = self.life.canonical_actor(account_id)
                actors.append(
                    {
                        "character": life_actor["character"],
                        "actor_id": life_actor["character"],
                        "account_id": account_id,
                        "qq": life_account["qq"],
                        "name": life_account["name"] or life_account["qq"],
                        "avatar_url": life_account["avatar_url"],
                        "context": None,
                        "life": True,
                        "persona_id": life_account["persona_id"],
                        "room_id": self._effective_room_id(
                            life_actor, self._indoor(), initialize=True
                        ),
                    }
                )

            context_by_account = {}
            for actor in contexts:
                identity = real_actor_identity(actor)
                if identity is not None:
                    context_by_account.setdefault(identity[:2], actor)
            accounts = []
            for row in self.db.execute(
                "SELECT id,platform_id,qq,name FROM accounts ORDER BY platform_id,qq"
            ):
                context = context_by_account.get((row["platform_id"], row["qq"]))
                life_account = life_accounts.get(row["id"], {})
                accounts.append(
                    {
                        "id": row["id"],
                        "platform_id": row["platform_id"],
                        "qq": row["qq"],
                        "name": row["name"],
                        "avatar_url": f"https://q1.qlogo.cn/g?b=qq&nk={row['qq']}&s=100",
                        "context": context,
                        "actor_id": f"life:{row['id']}"
                        if life_account.get("enabled")
                        else (context["character"] if context else ""),
                        "life_enabled": bool(life_account.get("enabled")),
                        "life": life_account,
                    }
                )

            states = []
            if contexts:
                for definition in definitions:
                    if definition["id"] in life_ids:
                        continue
                    rows_by_scope = {}
                    for actor in contexts:
                        scope_key = self.scope_key(definition, actor)
                        rows_by_scope.setdefault(scope_key, actor)
                    for scope_key, actor in sorted(rows_by_scope.items()):
                        row = self._row(definition, actor)
                        value = json.loads(row["value"])
                        stage = stage_for(definition, value)
                        states.append(
                            {
                                "key": dump([definition["id"], scope_key]),
                                "scope_key": scope_key,
                                "context": actor,
                                "unbound": False,
                                "id": definition["id"],
                                "value": value,
                                "stage": stage["name"],
                                "stage_description": stage["description"],
                                "updated": row["updated"],
                                "reason": row["reason"],
                                "kind": "state",
                                "computed": False,
                            }
                        )
            else:
                for definition in definitions:
                    if definition["id"] in life_ids:
                        continue
                    value = definition["initial"]
                    stage = stage_for(definition, value)
                    states.append(
                        {
                            "key": dump([definition["id"], "unbound"]),
                            "scope_key": "",
                            "context": None,
                            "unbound": True,
                            "id": definition["id"],
                            "value": value,
                            "stage": stage["name"],
                            "stage_description": stage["description"],
                            "updated": None,
                            "reason": "初始化",
                            "kind": "state",
                            "computed": False,
                        }
                    )

            if contexts:
                for group in groups:
                    if group["id"] in life_group_ids:
                        continue
                    rows_by_scope = {}
                    for actor in contexts:
                        scope_key = self.scope_key(group, actor)
                        rows_by_scope.setdefault(scope_key, actor)
                    for _, actor in sorted(rows_by_scope.items()):
                        states.append(self._group_state_row(group, definitions, actor))
            else:
                for group in groups:
                    if group["id"] in life_group_ids or any(
                        factor["state_id"] in life_ids for factor in group["factors"]
                    ):
                        continue
                    states.append(
                        self._group_state_row(group, definitions, unbound=True)
                    )

            for account_id, life_account in sorted(life_accounts.items()):
                if not life_account["enabled"]:
                    continue
                life_actor = self.life.canonical_actor(account_id)
                for definition in definitions:
                    if definition["id"] not in life_ids:
                        continue
                    row = self._row(definition, life_actor)
                    value = json.loads(row["value"])
                    stage = stage_for(definition, value)
                    scope_key = self.scope_key(definition, life_actor)
                    states.append(
                        {
                            "key": dump([definition["id"], scope_key]),
                            "scope_key": scope_key,
                            "context": None,
                            "account_id": account_id,
                            "unbound": False,
                            "id": definition["id"],
                            "value": value,
                            "stage": stage["name"],
                            "stage_description": stage["description"],
                            "updated": row["updated"],
                            "reason": row["reason"],
                            "kind": "state",
                            "computed": False,
                        }
                    )
                for group in groups:
                    if group["id"] not in life_group_ids:
                        continue
                    state = self._group_state_row(group, definitions, life_actor)
                    state["context"] = None
                    state["account_id"] = account_id
                    state["unbound"] = False
                    states.append(state)

            schedule = self._schedule(definitions)
            indoor = self._indoor()
            indoor_rooms = self._room_map(indoor)
            character_ids = set()
            occupants = []
            if contexts:
                for character, actor in sorted(actors_by_character.items()):
                    identity = real_actor_identity(actor)
                    if identity is None:
                        continue
                    _, qq, persona = identity
                    account_id = f"{identity[0]}:{identity[1]}"
                    if self.life.is_enabled(account_id):
                        continue
                    room_actor = self._indoor_actor(actor)
                    room_id = self._effective_room_id(
                        room_actor, indoor, initialize=True
                    )
                    character_ids.add(room_actor["character"])
                    occupants.append(
                        {
                            "character": character,
                            "actor_id": character,
                            "account_id": account_id,
                            "qq": qq,
                            "name": persona,
                            "avatar_url": f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100",
                            "room_id": room_id,
                            "group_id": indoor_rooms[room_id]["group_id"]
                            if room_id
                            else "",
                            "location_path": room_path(indoor, room_id),
                            "life": False,
                        }
                    )

            for account_id, life_account in sorted(life_accounts.items()):
                if not life_account["enabled"]:
                    continue
                life_actor = self.life.canonical_actor(account_id)
                room_id = self._effective_room_id(life_actor, indoor, initialize=True)
                character_ids.add(life_actor["character"])
                occupants.append(
                    {
                        "character": life_actor["character"],
                        "actor_id": life_actor["character"],
                        "account_id": account_id,
                        "qq": life_account["qq"],
                        "name": life_account["name"] or life_account["qq"],
                        "avatar_url": life_account["avatar_url"],
                        "room_id": room_id,
                        "group_id": indoor_rooms[room_id]["group_id"]
                        if room_id
                        else "",
                        "location_path": room_path(indoor, room_id),
                        "life": True,
                        "current_activity": life_account["current_activity"],
                    }
                )

            selected_room_actor = (
                self.life.canonical_actor(selected_account_id)
                if selected_account_id and self.life.is_enabled(selected_account_id)
                else selected
            )
            if selected_room_actor is not None:
                indoor_runtime = self._indoor_runtime(
                    selected_room_actor, indoor, character_ids=character_ids
                )
            else:
                indoor_runtime = {
                    "room_id": "",
                    "room": None,
                    "exits": [],
                    "occupancy": {room["id"]: 0 for room in indoor["rooms"]},
                    "group_id": "",
                    "location_path": "",
                    "group_path": [],
                }
            indoor_runtime["occupants"] = occupants

            return {
                "definitions": definitions,
                "groups": groups,
                "states": states,
                "categories": self._config("categories"),
                "context": selected,
                "selected_account_id": selected_account_id,
                "contexts": contexts,
                "actors": actors,
                "accounts": accounts,
                "schedule": schedule,
                "schedule_runtime": schedule_runtime(schedule, self.clock()),
                "indoor": indoor,
                "indoor_runtime": indoor_runtime,
                "life": life_data,
            }

    def adjust(self, state_id, actor, value=None, reset=False):
        actor = actor_context(actor)
        with self.transaction():
            definitions = self._config("states")
            before = self._advance(definitions, actor)
            state = next((s for s in definitions if s["id"] == state_id), None)
            if state is None:
                raise StateError("状态不存在")
            value = validate_value(state, state["initial"] if reset else value)
            self._write_value(state, actor, value, "手动重置" if reset else "手动调整")
            self._thresholds(definitions, actor, before, {**before, state_id: value})
            return self._snapshot(definitions, actor)

    def execute(
        self, state_id, rule_id, actor, *, source="tool", evidence="", token=""
    ):
        actor = actor_context(actor)
        with self.transaction():
            definitions = self._config("states")
            before = self._advance(definitions, actor)
            state = next((s for s in definitions if s["id"] == state_id), None)
            if state is None or not state["enabled"]:
                raise StateError("状态不存在或已停用")
            field = "events" if source == "semantic" else "interactions"
            item = next((r for r in state[field] if r["id"] == rule_id), None)
            if item is None:
                raise StateError("交互或事件不存在")
            if source == "semantic" and (
                item["trigger"] != "semantic" or not evidence.strip()
            ):
                raise StateError("仅能报告已配置的语义事件，并需要提供本轮对话依据")
            if source == "user" and not item["allow_user"]:
                raise StateError("此交互不允许通过普通用户指令执行")
            dedupe = dump([actor, source, state_id, rule_id, token]) if token else ""
            previous = (
                self.db.execute(
                    "SELECT result FROM seen WHERE token=?", (dedupe,)
                ).fetchone()
                if dedupe
                else None
            )
            if previous:
                return {
                    "ok": True,
                    "duplicate": True,
                    "message": "本轮已执行，未重复结算",
                    "snapshot": self._snapshot(definitions, actor),
                }
            reason = f"{'对话事件' if source == 'semantic' else '交互'}：{item['name']}"
            if evidence:
                reason += f"（{evidence[:160]}）"
            after = self._effects(item, definitions, actor, before, reason)
            self._thresholds(definitions, actor, before, after)
            if dedupe:
                self.db.execute(
                    "INSERT INTO seen VALUES (?,?,?)",
                    (dedupe, item["name"], self.clock()),
                )
                self.db.execute(
                    "DELETE FROM seen WHERE token NOT IN (SELECT token FROM seen ORDER BY updated DESC LIMIT 1000)"
                )
            return {
                "ok": True,
                "duplicate": False,
                "message": f"已完成：{item['name']}",
                "snapshot": self._snapshot(definitions, actor),
            }

    @staticmethod
    def public(snapshot):
        """Return enabled base states and computed groups for LLM tools."""
        rows = {r["id"]: r for r in snapshot["states"]}
        states = [
            {
                "id": s["id"],
                "name": s["name"],
                "value": rows[s["id"]]["value"] if s["show_value"] else "不展示数值",
                "stage": rows[s["id"]]["stage"],
                "description": s["description"],
                "stage_description": rows[s["id"]]["stage_description"],
                "interactions": s["interactions"],
                "events": [e for e in s["events"] if e["trigger"] == "semantic"],
                "kind": "state",
                "computed": False,
            }
            for s in snapshot["definitions"]
            if s["enabled"]
        ]
        definitions = {state["id"]: state for state in snapshot["definitions"]}
        for group in snapshot.get("groups", []):
            if not group["enabled"]:
                continue
            row = rows[group["id"]]
            components = []
            for component in row.get("components", []):
                entry = {
                    "state_id": component["state_id"],
                    "name": component["name"],
                }
                if group["show_value"]:
                    entry.update(
                        score=component["score"],
                        weight=component["weight"],
                        weight_percent=component["weight_percent"],
                        contribution=component["contribution"],
                    )
                    if definitions[component["state_id"]]["show_value"]:
                        entry["value"] = component["value"]
                components.append(entry)
            states.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "value": row["value"] if group["show_value"] else "不展示数值",
                    "stage": row["stage"],
                    "description": group["description"],
                    "stage_description": row["stage_description"],
                    "interactions": [],
                    "events": [],
                    "kind": "group",
                    "computed": True,
                    "components": components,
                }
            )
        return states

    @staticmethod
    def describe(snapshot):
        rows = {r["id"]: r for r in snapshot["states"]}
        names = {state["id"]: state["name"] for state in snapshot["definitions"]}
        definitions = {state["id"]: state for state in snapshot["definitions"]}
        lines = []
        for state in snapshot["definitions"]:
            if not state["enabled"] or not state["inject"]:
                continue
            row = rows[state["id"]]
            value = row["value"]
            if isinstance(value, float):
                value = round(value, 3)
            values = {
                "name": state["name"],
                "value": dump(value),
                "stage": row["stage"],
                "description": " ".join(
                    filter(None, [state["description"], row["stage_description"]])
                ),
                "reason": row["reason"],
            }
            rendered = state["prompt"]
            if not state["show_value"]:
                rendered = rendered.replace("{value}；", "").replace("{value}", "")
            for key, item in values.items():
                rendered = rendered.replace("{" + key + "}", str(item))
            lines.append(f"[{state['id']}] {rendered}")
            for item in state["interactions"]:
                lines.append(
                    f"  可执行交互 {state['id']}/{item['id']}：{item['name']}。{item['description']}；条件={dump(item['conditions'])}；效果={dump(item['effects'])}"
                )
            for item in state["events"]:
                if item["trigger"] == "semantic":
                    lines.append(
                        f"  可报告事件 {state['id']}/{item['id']}：{item['name']}。{item['description']}"
                    )
        for group in snapshot.get("groups", []):
            if not group["enabled"] or not group["inject"]:
                continue
            row = rows[group["id"]]
            value = row["value"]
            if isinstance(value, float):
                value = round(value, 3)
            values = {
                "name": group["name"],
                "value": dump(value),
                "stage": row["stage"],
                "description": " ".join(
                    filter(None, [group["description"], row["stage_description"]])
                ),
                "reason": row["reason"],
            }
            rendered = group["prompt"]
            if not group["show_value"]:
                rendered = rendered.replace("{value}；", "").replace("{value}", "")
            for key, item in values.items():
                rendered = rendered.replace("{" + key + "}", str(item))
            lines.append(f"[{group['id']}] {rendered}")
            summaries = []
            for component in row["components"]:
                source = definitions[component["state_id"]]
                if group["show_value"] and source["show_value"]:
                    summaries.append(
                        f"{component['name']}={dump(component['value'])}，"
                        f"评分{component['score']:.2f}，"
                        f"权重{component['weight_percent']:.2f}%/"
                        f"贡献{component['contribution']:.2f}"
                    )
                else:
                    stage = stage_for(source, component["value"])
                    label = stage["name"] or "未分阶段"
                    summaries.append(f"{component['name']}（{label}）")
            if summaries:
                lines.append("  来源贡献：" + "；".join(summaries))
        runtime = snapshot.get("schedule_runtime", {})
        if runtime.get("active") or runtime.get("next"):
            lines.append(f"日程本地时间：{runtime['local_time']}")
            active = runtime.get("active")
            if active:
                lines.append(
                    f"当前日程：{active['name']}。{active['description']}；持续至 {active['end_at']}。"
                )
                if active["rates"]:
                    rates = "、".join(
                        f"{names.get(state_id, state_id)} {rate:+g}/小时"
                        for state_id, rate in active["rates"].items()
                    )
                    lines.append(
                        f"当前日程覆盖变化速率：{rates}。未列出的数值状态沿用基础速率。"
                    )
            upcoming = runtime.get("next")
            if upcoming:
                lines.append(
                    f"下一日程：{upcoming['name']}。{upcoming['description']}；{upcoming['start_at']} 至 {upcoming['end_at']}。"
                )
        indoor = snapshot.get("indoor", {})
        indoor_runtime = snapshot.get("indoor_runtime", {})
        room = indoor_runtime.get("room")
        if room:
            exits = indoor_runtime.get("exits", [])
            exit_text = "、".join(item["name"] for item in exits) or "无直接出口"
            description = f"；{room['description']}" if room.get("description") else ""
            lines.append(
                f"当前位置：{room['name']}{description}。可直接前往：{exit_text}。"
            )
            lines.append(
                "当前位置由插件记录；需要查看房间和出口时调用 brm_look，需要移动时调用 brm_move_to 并以工具结果为准。"
            )
        elif indoor.get("rooms"):
            lines.append(
                "环境已配置房间；使用 brm_look 查看起始位置和出口，使用 brm_move_to 移动并以工具结果为准。"
            )
        if not lines:
            return ""
        return (
            "<better_real_me>\n这是你此刻的生活状态，由插件维护。自然体现相关感受，无须逐项播报。\n只有已完成的行为才执行 brm_interact；计划、假设、引用不等于已完成。语义事件使用 brm_report_event 并提供本轮依据。以工具返回结果为准，不自行编造状态变化。\n"
            + "\n".join(lines)
            + "\n</better_real_me>"
        )
