"""Persistent, account-scoped autonomous life simulation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from . import life_presets, neighborhood_presets
from .indoor import shortest_path
from .schedule import _minutes, active_entry, boundaries, schedule_runtime
from .schema import (
    StateError,
    identifier,
    number,
    stage_for,
    text,
    validate_value,
)
from .schema import (
    condition as normalize_condition,
)

_MEMORY_STATUSES = {"disabled", "pending", "processing", "saved", "failed", "ignored"}
_ACTION_LIMIT = 512
_MAX_CATCHUP_EVENTS = 384
_MODEL_DECISION_GRACE = 30.0
_LIFE_PRESET_IDS = {neighborhood_presets.PRESET_ID}


def _iso(timestamp):
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _window(raw, index):
    if not isinstance(raw, dict):
        raise StateError("生活行动时间窗口格式错误")
    start, end = raw.get("start"), raw.get("end")
    _minutes(start, f"第{index}个窗口开始时间")
    _minutes(end, f"第{index}个窗口结束时间")
    days = raw.get("days", list(range(7)))
    if (
        not isinstance(days, list)
        or not days
        or any(
            isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6
            for day in days
        )
        or len(set(days)) != len(days)
    ):
        raise StateError("生活行动星期应为不重复的0至6列表")
    return {"start": start, "end": end, "days": sorted(days)}


def normalize_action(raw, definitions, room_ids, owned_ids):
    """Validate an editable life action against account-owned life states."""
    if not isinstance(raw, dict):
        raise StateError("生活行动必须为对象")
    action_id = identifier(raw.get("id"), "生活行动标识")
    windows = raw.get("windows", [])
    steps = raw.get("steps", [])
    conditions = raw.get("conditions", [])
    if not isinstance(windows, list):
        raise StateError("生活行动时间窗口必须为列表")
    if not isinstance(steps, list) or not steps or len(steps) > 12:
        raise StateError("生活行动必须包含1至12个步骤")
    if not isinstance(conditions, list):
        raise StateError("生活行动条件必须为列表")

    normalized_conditions = []
    for raw_condition in conditions:
        item = normalize_condition(raw_condition, definitions, action_id)
        if item["state"] not in owned_ids:
            raise StateError("生活后台行动只能引用生活内核拥有的状态")
        normalized_conditions.append(item)

    normalized_steps = []
    for step_index, raw_step in enumerate(steps, 1):
        if not isinstance(raw_step, dict):
            raise StateError("生活行动步骤格式错误")
        room_id = raw_step.get("room_id", "") or ""
        if room_id:
            room_id = identifier(room_id, "目标房间标识")
            if room_id not in room_ids:
                raise StateError(f"生活行动引用了不存在的房间：{room_id}")
        duration = raw_step.get("duration_minutes", 1)
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 1 <= duration <= 1440
        ):
            raise StateError(f"第{step_index}步时长必须为1至1440分钟")
        effects = raw_step.get("effects", [])
        rates = raw_step.get("rates", {})
        if not isinstance(effects, list) or not isinstance(rates, dict):
            raise StateError("生活行动效果或连续变化量格式错误")
        normalized_effects = []
        for effect in effects:
            if not isinstance(effect, dict):
                raise StateError("生活行动效果格式错误")
            state_id = effect.get("state")
            if state_id not in owned_ids or state_id not in definitions:
                raise StateError("生活后台行动只能修改生活内核拥有的状态")
            target = definitions[state_id]
            op, value = effect.get("op", "add"), effect.get("value")
            if op == "add" and target["type"] == "number":
                value = number(value, f"{target['name']}变化量")
            elif op == "set":
                value = validate_value(target, value)
            else:
                raise StateError("数值状态支持add/set，其他状态只支持set")
            normalized_effects.append({"state": state_id, "op": op, "value": value})
        normalized_rates = {}
        for state_id, rate in rates.items():
            target = definitions.get(state_id)
            if (
                state_id not in owned_ids
                or target is None
                or target["type"] != "number"
            ):
                raise StateError("生活连续变化量只能引用生活内核数值状态")
            normalized_rates[state_id] = number(rate, f"{target['name']}每小时变化量")
        normalized_steps.append(
            {
                "name": text(raw_step.get("name", ""), "步骤名称", True),
                "room_id": room_id,
                "duration_minutes": duration,
                "effects": normalized_effects,
                "rates": normalized_rates,
            }
        )

    preset_ids = raw.get("preset_ids", [])
    if (
        not isinstance(preset_ids, list)
        or any(
            not isinstance(item, str) or item not in _LIFE_PRESET_IDS
            for item in preset_ids
        )
        or len(preset_ids) != len(set(preset_ids))
    ):
        raise StateError("生活行动预设范围只能是街区生活或空列表")

    priority = raw.get("priority", 0)
    cooldown = raw.get("cooldown_minutes", 0)
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -10000 <= priority <= 10000
    ):
        raise StateError("生活行动优先级必须为-10000至10000的整数")
    if (
        isinstance(cooldown, bool)
        or not isinstance(cooldown, int)
        or not 0 <= cooldown <= 10080
    ):
        raise StateError("冷却时间必须为0至10080分钟")
    return {
        "id": action_id,
        "name": text(raw.get("name", ""), "生活行动名称", True),
        "description": text(raw.get("description", ""), "生活行动说明"),
        "category": text(raw.get("category", ""), "生活行动分类"),
        "preset_ids": list(preset_ids),
        "enabled": bool(raw.get("enabled", True)),
        "priority": priority,
        "cooldown_minutes": cooldown,
        "windows": [_window(item, index) for index, item in enumerate(windows, 1)],
        "conditions": normalized_conditions,
        "steps": normalized_steps,
        "finish_by_window": bool(raw.get("finish_by_window", False)),
        "memory_relevant": bool(raw.get("memory_relevant", False)),
    }


class LifeRuntime:
    """Owns autonomous account state without registering fake chat contexts."""

    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db
        with engine.transaction():
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS life_accounts (
                    account_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    preset_id TEXT NOT NULL DEFAULT '',
                    persona_id TEXT NOT NULL DEFAULT '',
                    memory_enabled INTEGER NOT NULL DEFAULT 0,
                    model_enabled INTEGER NOT NULL DEFAULT 0,
                    provider_id TEXT NOT NULL DEFAULT '',
                    last_tick REAL,
                    activity TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS life_cooldowns (
                    account_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    started REAL NOT NULL,
                    PRIMARY KEY(account_id, action_id)
                );
                CREATE TABLE IF NOT EXISTS life_events (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    body TEXT NOT NULL,
                    memory_status TEXT NOT NULL,
                    memory_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS life_events_account_time
                    ON life_events(account_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS life_events_memory_status
                    ON life_events(memory_status, occurred_at);
            """)
            self.db.execute(
                "UPDATE life_events SET memory_status='pending' WHERE memory_status='processing'"
            )
            if self._manifest():
                life_presets.migrate_ingredient_replenish_action(engine)

    def _manifest(self):
        return self.engine._config("life_manifest") or {}

    def migrate_legacy_preset_data(self):
        """Rewrite the retired home preset ID without settling user activity."""
        legacy_id = life_presets.LEGACY_PRESET_ID
        preset_id = neighborhood_presets.PRESET_ID
        with self.engine.transaction():
            actions = self.engine._config("life_actions")
            actions_changed = False
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    preset_ids = action.get("preset_ids")
                    if not isinstance(preset_ids, list) or legacy_id not in preset_ids:
                        continue
                    migrated = []
                    for item in preset_ids:
                        item = preset_id if item == legacy_id else item
                        if item not in migrated:
                            migrated.append(item)
                    if migrated != preset_ids:
                        action["preset_ids"] = migrated
                        actions_changed = True
            if actions_changed:
                self.engine._save_config("life_actions", actions)

            legacy_accounts = self.db.execute(
                "SELECT account_id FROM life_accounts WHERE preset_id=?",
                (legacy_id,),
            ).fetchall()
            if legacy_accounts:
                self.db.execute(
                    "UPDATE life_accounts SET preset_id=?,revision=revision+1 WHERE preset_id=?",
                    (preset_id, legacy_id),
                )

            active = self.db.execute(
                "SELECT 1 FROM life_accounts WHERE enabled=1 LIMIT 1"
            ).fetchone()
            if active:
                neighborhood_presets.install(self.engine)

    def owned_state_ids(self):
        return set(self._manifest().get("state_ids", []))

    def owned_group_ids(self):
        return set(self._manifest().get("group_ids", []))

    def _account_id(self, account_id, *, require_known=True):
        raw = str(account_id or "").strip()
        platform, sep, qq = raw.partition(":")
        platform, qq = self.engine._account_identity(platform, qq) if sep else ("", "")
        canonical = f"{platform}:{qq}"
        if require_known:
            with self.engine.lock:
                known = self.db.execute(
                    "SELECT 1 FROM accounts WHERE id=?", (canonical,)
                ).fetchone()
            if not known:
                raise StateError("请选择已识别的真实QQ账号")
        return canonical

    @staticmethod
    def canonical_actor(account_id):
        return {
            "character": f"life:{account_id}",
            "conversation": "life",
            "user": f"life:{account_id}",
        }

    @staticmethod
    def _real_identity(actor):
        character = str((actor or {}).get("character", ""))
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

    def actor(self, account_id):
        account_id = self._account_id(account_id)
        row = self.db.execute(
            "SELECT enabled FROM life_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None or not row["enabled"]:
            raise StateError("该QQ账号尚未启用生活内核")
        return self.canonical_actor(account_id)

    def is_enabled(self, account_id):
        with self.engine.lock:
            row = self.db.execute(
                "SELECT enabled FROM life_accounts WHERE account_id=?",
                (str(account_id),),
            ).fetchone()
        return bool(row and row["enabled"])

    def is_configured(self, account_id):
        return self._settings(str(account_id)) is not None

    def account_for_actor(self, actor):
        character = str((actor or {}).get("character", ""))
        if character.startswith("life:"):
            account_id = character[len("life:") :]
            try:
                return self._account_id(account_id)
            except StateError:
                return ""
        platform, separator, remainder = character.partition(":")
        if not separator:
            return ""
        qq, separator, persona = remainder.partition(":")
        if (
            not separator
            or not qq.isascii()
            or not qq.isdigit()
            or not qq.strip("0")
            or not persona
        ):
            return ""
        identity = (platform, qq, persona)
        return f"{identity[0]}:{identity[1]}" if identity else ""

    def _settings(self, account_id):
        with self.engine.lock:
            return self.db.execute(
                "SELECT * FROM life_accounts WHERE account_id=?", (account_id,)
            ).fetchone()

    @staticmethod
    def _pause_activity(activity, timestamp):
        if not activity or activity.get("paused"):
            return activity
        activity["paused"] = True
        activity["paused_at"] = timestamp
        activity["remaining_seconds"] = max(0.0, activity["ends_at"] - timestamp)
        activity["remaining_to_arrive"] = max(
            0.0, activity.get("arrives_at", timestamp) - timestamp
        )
        activity["travel_remaining"] = [
            {
                "room_id": item["room_id"],
                "seconds": max(0.0, item["at"] - timestamp),
            }
            for item in activity.get("travel_events", [])
            if item["at"] > timestamp
        ]
        activity["travel_events"] = []
        return activity

    @staticmethod
    def _action_allowed(action, preset_id):
        preset_ids = action.get("preset_ids") or []
        return not preset_ids or preset_id in preset_ids

    def _actions_for_account(self, account_id, actions=None):
        settings = self._settings(account_id)
        if settings is None:
            return []
        actions = self._all_actions() if actions is None else actions
        preset_id = settings["preset_id"] or life_presets.PRESET_ID
        return [item for item in actions if self._action_allowed(item, preset_id)]

    def _all_actions(self, definitions=None, indoor=None):
        definitions = (
            self.engine._config("states") if definitions is None else definitions
        )
        indoor = self.engine._indoor() if indoor is None else indoor
        manifest = self._manifest()
        owned = set(manifest.get("state_ids", []))
        raw_actions = self.engine._config("life_actions")
        if not isinstance(raw_actions, list):
            raise StateError("生活行动配置格式错误")
        normalized = [
            normalize_action(
                item,
                {s["id"]: s for s in definitions},
                {r["id"] for r in indoor["rooms"]},
                owned,
            )
            for item in raw_actions
        ]
        ids = [item["id"] for item in normalized]
        if len(ids) != len(set(ids)):
            raise StateError("生活行动标识不能重复")
        return normalized

    def configure_preset(self, preset_id, enabled, account_id, persona_id=""):
        if preset_id not in _LIFE_PRESET_IDS:
            raise StateError("未知生活预设")
        if not isinstance(enabled, bool):
            raise StateError("生活预设开关必须为布尔值")
        account_id = self._account_id(account_id)
        now = self.engine.clock()
        with self.engine.transaction():
            row = self._settings(account_id)
            if not enabled:
                if row is None:
                    selected_persona = str(persona_id or "").strip()
                    if not selected_persona:
                        selected_persona = (
                            self._discovered_persona(account_id) or "default"
                        )
                    self.db.execute(
                        "INSERT INTO life_accounts(account_id,enabled,preset_id,persona_id,last_tick,activity,revision,updated) "
                        "VALUES (?,0,?,?,NULL,NULL,1,?)",
                        (account_id, preset_id, selected_persona, now),
                    )
                    return self.get_overview()
                was_enabled = bool(row["enabled"])
                if was_enabled:
                    self._settle_account(account_id, now, deterministic=True)
                    row = self._settings(account_id)
                activity = json.loads(row["activity"]) if row["activity"] else None
                actions = self._all_actions() if self._manifest() else []
                action = next(
                    (
                        item
                        for item in actions
                        if item["id"] == (activity or {}).get("action_id")
                    ),
                    None,
                )
                if activity and (
                    action is None or not self._action_allowed(action, preset_id)
                ):
                    # A stale step is discarded without effects or movement.
                    activity = None
                else:
                    activity = self._pause_activity(activity, now)
                selected_persona = str(persona_id or "").strip()
                if not selected_persona:
                    selected_persona = (
                        row["persona_id"]
                        or self._discovered_persona(account_id)
                        or "default"
                    )
                last_tick = now if was_enabled else row["last_tick"]
                self.db.execute(
                    "UPDATE life_accounts SET enabled=0,preset_id=?,persona_id=?,activity=?,last_tick=?,revision=revision+1,updated=? WHERE account_id=?",
                    (
                        preset_id,
                        selected_persona,
                        self._dump(activity) if activity else None,
                        last_tick,
                        now,
                        account_id,
                    ),
                )
                return self.get_overview()

            if row is not None and row["enabled"]:
                # Settle through the current time before refreshing settings.
                self._settle_account(account_id, now, deterministic=True)
                row = self._settings(account_id)

            manifest = life_presets.install(self.engine)
            if preset_id == "neighborhood_life":
                from . import neighborhood_presets

                neighborhood_presets.install(self.engine)
            actions = self._all_actions()
            action_index = {item["id"]: item for item in actions}
            row = self._settings(account_id)
            selected_persona = str(persona_id or "").strip()
            if not selected_persona:
                selected_persona = (
                    (row["persona_id"] if row else "")
                    or self._discovered_persona(account_id)
                    or "default"
                )

            def compatible_activity(activity):
                if not activity:
                    return True
                action = action_index.get(activity.get("action_id"))
                return bool(action and self._action_allowed(action, preset_id))

            if row is not None and row["enabled"]:
                activity = json.loads(row["activity"]) if row["activity"] else None
                if activity and not compatible_activity(activity):
                    # Cancel without completing the current step. Location rows
                    # already contain every travel event settled above.
                    activity = None
                self.db.execute(
                    "UPDATE life_accounts SET persona_id=?,preset_id=?,activity=?,last_tick=?,revision=revision+1,updated=? WHERE account_id=?",
                    (
                        selected_persona,
                        preset_id,
                        self._dump(activity) if activity else None,
                        now,
                        now,
                        account_id,
                    ),
                )
                return self.get_overview()

            actor = self.canonical_actor(account_id)
            definitions = self.engine._config("states")
            indexed = {item["id"]: item for item in definitions}
            for state_id in manifest["state_ids"]:
                definition = indexed.get(state_id)
                if definition is None:
                    continue
                state_row = self.engine._row(definition, actor)
                # A never-used installation is anchored at enable time. Existing
                # values and edits survive pause/resume and repeated enable.
                if state_row["reason"] == "初始化":
                    self._write_state(
                        state_id, actor, json.loads(state_row["value"]), "初始状态", now
                    )
            previous = self.db.execute(
                "SELECT activity FROM life_accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            activity = (
                json.loads(previous["activity"])
                if previous and previous["activity"]
                else None
            )
            if activity and not compatible_activity(activity):
                activity = None
            elif activity and activity.get("paused"):
                activity["paused"] = False
                remaining = max(1.0, float(activity.pop("remaining_seconds", 1.0)))
                to_arrive = max(0.0, float(activity.pop("remaining_to_arrive", 0.0)))
                activity["started_at"] = now
                activity["step_started_at"] = now + to_arrive
                activity["arrives_at"] = now + to_arrive
                activity["ends_at"] = now + remaining
                activity["travel_events"] = [
                    {"room_id": item["room_id"], "at": now + float(item["seconds"])}
                    for item in activity.pop("travel_remaining", [])
                ]
            else:
                activity = None

            self.db.execute(
                "INSERT INTO life_accounts(account_id,enabled,preset_id,persona_id,last_tick,activity,revision,updated) "
                "VALUES (?,1,?,?,?, ?,1,?) ON CONFLICT(account_id) DO UPDATE SET "
                "enabled=1,preset_id=excluded.preset_id,persona_id=excluded.persona_id,last_tick=excluded.last_tick,"
                "activity=excluded.activity,revision=life_accounts.revision+1,updated=excluded.updated",
                (
                    account_id,
                    preset_id,
                    selected_persona,
                    now,
                    self._dump(activity) if activity else None,
                    now,
                ),
            )
            self._seed_location(account_id, selected_persona)
            return self.get_overview()

    def _discovered_persona(self, account_id):
        platform, qq = account_id.split(":", 1)
        rows = self.db.execute(
            "SELECT body FROM contexts ORDER BY updated DESC"
        ).fetchall()
        for row in rows:
            try:
                actor = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            identity = self._real_identity(actor)
            if identity and identity[:2] == (platform, qq):
                return identity[2]
        return ""

    def _seed_location(self, account_id, persona_id):
        actor = self.canonical_actor(account_id)
        exists = self.db.execute(
            "SELECT 1 FROM indoor_locations WHERE character_key=?",
            (actor["character"],),
        ).fetchone()
        if exists:
            return
        platform, qq = account_id.split(":", 1)
        source = f"{platform}:{qq}:{persona_id}"
        old = self.db.execute(
            "SELECT room_id FROM indoor_locations WHERE character_key=?", (source,)
        ).fetchone()
        room_id = old[0] if old else self.engine._indoor()["initial_room_id"]
        if room_id:
            self.db.execute(
                "INSERT INTO indoor_locations(character_key,room_id,updated) VALUES (?,?,?)",
                (actor["character"], room_id, self.engine.clock()),
            )

    def update_settings(self, account_id, **settings):
        account_id = self._account_id(account_id)
        allowed = {"memory_enabled", "model_enabled", "provider_id", "persona_id"}
        unknown = set(settings) - allowed
        if unknown:
            raise StateError(f"未知生活设置：{min(unknown)}")
        with self.engine.transaction():
            row = self._settings(account_id)
            if row is None:
                raise StateError("请先为该QQ账号启用生活预设")
            values = {
                "memory_enabled": bool(row["memory_enabled"]),
                "model_enabled": bool(row["model_enabled"]),
                "provider_id": row["provider_id"],
                "persona_id": row["persona_id"],
            }
            for key, value in settings.items():
                if key.endswith("_enabled"):
                    if not isinstance(value, bool):
                        raise StateError(f"{key}必须为布尔值")
                    values[key] = value
                elif key == "provider_id":
                    values[key] = text(value, "模型提供方")
                else:
                    values[key] = text(value, "固定人格")
            self.db.execute(
                "UPDATE life_accounts SET memory_enabled=?,model_enabled=?,provider_id=?,persona_id=?,revision=revision+1,updated=? WHERE account_id=?",
                (
                    int(values["memory_enabled"]),
                    int(values["model_enabled"]),
                    values["provider_id"],
                    values["persona_id"],
                    self.engine.clock(),
                    account_id,
                ),
            )
        return self.get_overview()

    def save_action(self, action):
        definitions = self.engine._config("states")
        indoor = self.engine._indoor()
        normalized = normalize_action(
            action,
            {s["id"]: s for s in definitions},
            {r["id"] for r in indoor["rooms"]},
            self.owned_state_ids(),
        )
        with self.engine.transaction():
            actions = self.engine._config("life_actions")
            current = next(
                (item for item in actions if item.get("id") == normalized["id"]), None
            )
            if current is None and len(actions) >= _ACTION_LIMIT:
                raise StateError("生活行动数量已达上限")
            actions = [
                normalized if item.get("id") == normalized["id"] else item
                for item in actions
            ]
            if current is None:
                actions.append(normalized)
            self.engine._save_config("life_actions", actions)
            self._bump_all_revisions()
        return self.get_overview()

    def delete_action(self, action_id):
        action_id = identifier(action_id, "生活行动标识")
        with self.engine.transaction():
            actions = self.engine._config("life_actions")
            if not any(item.get("id") == action_id for item in actions):
                raise StateError("生活行动不存在")
            self.engine._save_config(
                "life_actions",
                [item for item in actions if item.get("id") != action_id],
            )
            self.db.execute(
                "DELETE FROM life_cooldowns WHERE action_id=?", (action_id,)
            )
            self._bump_all_revisions()
        return self.get_overview()

    def _bump_all_revisions(self):
        self.db.execute("UPDATE life_accounts SET revision=revision+1")

    def _state_values(self, account_id, definitions=None):
        definitions = (
            self.engine._config("states") if definitions is None else definitions
        )
        actor = self.canonical_actor(account_id)
        owned = self.owned_state_ids()
        values, updated = {}, {}
        indexed = {item["id"]: item for item in definitions}
        for state_id in sorted(owned):
            state = indexed.get(state_id)
            if state is None:
                continue
            row = self.engine._row(state, actor)
            values[state_id] = json.loads(row["value"])
            updated[state_id] = row["updated"]
        return actor, indexed, values, updated

    def _state_values_readonly(self, account_id, definitions):
        """Read life values without initializing missing runtime rows."""
        actor = self.canonical_actor(account_id)
        indexed = {item["id"]: item for item in definitions}
        values = {}
        for state_id in sorted(self.owned_state_ids()):
            state = indexed.get(state_id)
            if state is None:
                continue
            scope_key = self.engine.scope_key(state, actor)
            row = self.db.execute(
                "SELECT value FROM runtime WHERE state_id=? AND scope_key=?",
                (state_id, scope_key),
            ).fetchone()
            raw_value = json.loads(row[0]) if row else state["initial"]
            values[state_id] = validate_value(state, raw_value)
        return values

    def _write_state(self, state_id, actor, value, reason, timestamp):
        definition = next(
            (item for item in self.engine._config("states") if item["id"] == state_id),
            None,
        )
        if definition is None:
            return
        key = self.engine.scope_key(definition, actor)
        self.db.execute(
            "UPDATE runtime SET value=?,updated=?,reason=? WHERE state_id=? AND scope_key=?",
            (self._dump(value), timestamp, reason, state_id, key),
        )

    def _schedule(self, definitions=None):
        return self.engine._schedule(definitions)

    def _rate_at(self, state, timestamp, schedule_entry, activity, actor):
        rate = state["rate_per_hour"]
        if schedule_entry and state["id"] in schedule_entry["rates"]:
            rate = schedule_entry["rates"][state["id"]]
        if activity and timestamp >= activity.get("arrives_at", activity["started_at"]):
            rate = activity["rates"].get(state["id"], rate)
        return rate

    def _integrate(self, account_id, start, end, activity=None, definitions=None):
        if end <= start:
            return
        definitions = (
            self.engine._config("states") if definitions is None else definitions
        )
        actor, indexed, values, updated = self._state_values(account_id, definitions)
        schedule = self._schedule(definitions)
        owned = set(values)
        points = {start, end}
        points.update(boundaries(schedule, start, end))
        points.update(t for t in updated.values() if start < t < end)
        timeline = sorted(points)
        reasons = {state_id: "生活内核：时间经过" for state_id in owned}
        for segment_start, segment_end in pairwise(timeline):
            if segment_end <= segment_start:
                continue
            entry = active_entry(schedule, (segment_start + segment_end) / 2)
            eligible = {
                state_id for state_id in owned if updated[state_id] <= segment_start
            }
            before, after = dict(values), dict(values)
            elapsed_hours = (segment_end - segment_start) / 3600
            for state_id in eligible:
                state = indexed[state_id]
                if not state["enabled"] or state["type"] != "number":
                    continue
                rate = self._rate_at(state, segment_start, entry, activity, actor)
                if rate:
                    after[state_id] = self.engine._bounded(
                        state, before[state_id] + elapsed_hours * rate
                    )
            after, event_reasons = self.engine._thresholds(
                definitions,
                actor,
                before,
                after,
                eligible_ids=eligible,
                available_ids=eligible,
                persist=False,
                return_reasons=True,
            )
            reasons.update(event_reasons)
            values = after
            for state_id in eligible:
                if values[state_id] != before[state_id]:
                    self._write_state(
                        state_id,
                        actor,
                        values[state_id],
                        reasons[state_id],
                        segment_end,
                    )
                    updated[state_id] = segment_end

    def _local_zone(self, schedule):
        return ZoneInfo(schedule["timezone"])

    def _window_occurrence(self, action, timestamp, schedule):
        if not action["windows"]:
            return (float("-inf"), float("inf"))
        zone = self._local_zone(schedule)
        local = datetime.fromtimestamp(timestamp, zone)
        for offset in (-1, 0):
            day = local.date() + timedelta(days=offset)
            for window in action["windows"]:
                if day.weekday() not in window["days"]:
                    continue
                start_minute, end_minute = (
                    _minutes(window["start"], "窗口开始"),
                    _minutes(window["end"], "窗口结束"),
                )
                start_dt = datetime.combine(day, datetime.min.time(), zone) + timedelta(
                    minutes=start_minute
                )
                end_day = day + timedelta(days=1) if end_minute <= start_minute else day
                end_dt = datetime.combine(
                    end_day, datetime.min.time(), zone
                ) + timedelta(minutes=end_minute)
                start_ts, end_ts = start_dt.timestamp(), end_dt.timestamp()
                if start_ts <= timestamp < end_ts:
                    return start_ts, end_ts
        return None

    def _windows_allow(self, action, timestamp, schedule):
        return self._window_occurrence(action, timestamp, schedule) is not None

    def _action_conditions(self, action, values):
        for item in action["conditions"]:
            left, right = values[item["state"]], item["value"]
            op = item["op"]
            if (op == "eq" and left != right) or (op == "ne" and left == right):
                return False
            if (op == "lt" and not left < right) or (op == "lte" and not left <= right):
                return False
            if (op == "gt" and not left > right) or (op == "gte" and not left >= right):
                return False
        return True

    def _cooldown_ready(self, account_id, action, timestamp):
        row = self.db.execute(
            "SELECT started FROM life_cooldowns WHERE account_id=? AND action_id=?",
            (account_id, action["id"]),
        ).fetchone()
        return not row or timestamp >= row["started"] + action["cooldown_minutes"] * 60

    def _candidates(self, account_id, timestamp, definitions=None, actions=None):
        settings = self._settings(account_id)
        if settings is None or not settings["enabled"]:
            return []
        definitions = (
            self.engine._config("states") if definitions is None else definitions
        )
        actions = self._all_actions(definitions) if actions is None else actions
        actions = self._actions_for_account(account_id, actions)
        _, _, values, _ = self._state_values(account_id, definitions)
        schedule = self._schedule(definitions)
        candidates = [
            item
            for item in actions
            if item["enabled"]
            and self._windows_allow(item, timestamp, schedule)
            and self._cooldown_ready(account_id, item, timestamp)
            and self._action_conditions(item, values)
        ]
        return sorted(candidates, key=lambda item: (-item["priority"], item["id"]))

    def available_actions(self, account_id, timestamp=None):
        """Return actions eligible to start now for an enabled account."""
        account_id = self._account_id(account_id)
        timestamp = self.engine.clock() if timestamp is None else float(timestamp)
        with self.engine.lock:
            settings = self._settings(account_id)
            if settings is None or not settings["enabled"] or settings["activity"]:
                return []
            actions = self._candidates(account_id, timestamp)
            return copy.deepcopy(actions)

    def available_action_windows(self, account_id, timestamp=None):
        """Return today's current-state-eligible action start intervals.

        The method is read-only: it does not settle life, start actions, or
        mutate settings. Conditions are evaluated against the account's current
        state, and intervals are clipped to the account's local calendar day.
        """
        account_id = self._account_id(account_id)
        timestamp = self.engine.clock() if timestamp is None else float(timestamp)
        with self.engine.lock:
            settings = self._settings(account_id)
            if settings is None or not settings["enabled"]:
                return {"date": "", "timezone": "", "busy": False, "windows": []}
            busy = bool(settings["activity"])
            definitions = self.engine._config("states")
            actions = self._actions_for_account(
                account_id, self._all_actions(definitions)
            )
            values = self._state_values_readonly(account_id, definitions)
            schedule = self._schedule(definitions)
            zone = self._local_zone(schedule)
            local_now = datetime.fromtimestamp(timestamp, zone)
            day_start = datetime.combine(local_now.date(), datetime.min.time(), zone)
            day_end = datetime.combine(
                local_now.date() + timedelta(days=1), datetime.min.time(), zone
            )
            day_start_ts, day_end_ts = day_start.timestamp(), day_end.timestamp()
            now = max(timestamp, day_start_ts)
            result = []

            for action in actions:
                if not action["enabled"] or not self._action_conditions(action, values):
                    continue
                cooldown = self.db.execute(
                    "SELECT started FROM life_cooldowns WHERE account_id=? AND action_id=?",
                    (account_id, action["id"]),
                ).fetchone()
                ready_at = now
                if cooldown:
                    ready_at = max(
                        ready_at,
                        cooldown["started"] + action["cooldown_minutes"] * 60,
                    )
                if ready_at >= day_end_ts:
                    continue

                occurrences = []
                if not action["windows"]:
                    occurrences.append((day_start_ts, day_end_ts))
                else:
                    for offset in (-1, 0):
                        start_day = local_now.date() + timedelta(days=offset)
                        for window in action["windows"]:
                            if start_day.weekday() not in window["days"]:
                                continue
                            start_minute = _minutes(window["start"], "窗口开始")
                            end_minute = _minutes(window["end"], "窗口结束")
                            start_dt = datetime.combine(
                                start_day, datetime.min.time(), zone
                            ) + timedelta(minutes=start_minute)
                            end_day = (
                                start_day + timedelta(days=1)
                                if end_minute <= start_minute
                                else start_day
                            )
                            end_dt = datetime.combine(
                                end_day, datetime.min.time(), zone
                            ) + timedelta(minutes=end_minute)
                            occurrences.append(
                                (start_dt.timestamp(), end_dt.timestamp())
                            )

                for start_ts, end_ts in occurrences:
                    start_ts = max(start_ts, ready_at, day_start_ts)
                    end_ts = min(end_ts, day_end_ts)
                    if start_ts >= end_ts or end_ts <= timestamp:
                        continue
                    local_start = datetime.fromtimestamp(start_ts, zone)
                    local_end = datetime.fromtimestamp(end_ts, zone)
                    result.append(
                        {
                            "action_id": action["id"],
                            "name": action["name"],
                            "description": action["description"],
                            "category": action["category"],
                            "priority": action["priority"],
                            "start_at": local_start.isoformat(timespec="minutes"),
                            "end_at": local_end.isoformat(timespec="minutes"),
                            "available_now": (
                                not busy and start_ts <= timestamp < end_ts
                            ),
                        }
                    )
            result.sort(
                key=lambda item: (
                    item["start_at"],
                    -item["priority"],
                    item["action_id"],
                )
            )
            return {
                "date": local_now.date().isoformat(),
                "timezone": schedule["timezone"],
                "busy": busy,
                "windows": result,
            }

    def _next_opportunity(
        self, account_id, timestamp, target, actions, schedule, definitions
    ):
        actions = self._actions_for_account(account_id, actions)
        points = []
        zone = self._local_zone(schedule)
        local = datetime.fromtimestamp(timestamp, zone)
        for action in actions:
            if not action["enabled"]:
                continue
            cooldown = self.db.execute(
                "SELECT started FROM life_cooldowns WHERE account_id=? AND action_id=?",
                (account_id, action["id"]),
            ).fetchone()
            if cooldown:
                due = cooldown["started"] + action["cooldown_minutes"] * 60
                if timestamp < due <= target:
                    points.append(due)
            for offset in range(-1, 10):
                day = local.date() + timedelta(days=offset)
                for window in action["windows"]:
                    if day.weekday() not in window["days"]:
                        continue
                    minute = _minutes(window["start"], "窗口开始")
                    due = (
                        datetime.combine(day, datetime.min.time(), zone)
                        + timedelta(minutes=minute)
                    ).timestamp()
                    if timestamp < due <= target:
                        points.append(due)
        points.extend(boundaries(schedule, timestamp, target))
        actor, indexed, values, _ = self._state_values(account_id, definitions)
        entry = active_entry(schedule, timestamp + 0.001)
        for action in actions:
            if not action["enabled"]:
                continue
            for clause in action["conditions"]:
                state_id, op, target_value = (
                    clause["state"],
                    clause["op"],
                    clause["value"],
                )
                state = indexed[state_id]
                if state["type"] != "number":
                    continue
                value = values[state_id]
                rate = self._rate_at(state, timestamp, entry, None, actor)
                if rate == 0:
                    continue
                crossing = None
                if op in {"gt", "gte"} and value < target_value and rate > 0:
                    crossing = timestamp + (target_value - value) / rate * 3600
                    if op == "gt":
                        crossing += 0.01
                elif op in {"lt", "lte"} and value > target_value and rate < 0:
                    crossing = timestamp + (value - target_value) / -rate * 3600
                    if op == "lt":
                        crossing += 0.01
                if crossing is not None and timestamp < crossing <= target:
                    points.append(crossing)
        return min(points) if points else target

    def _travel_plan(self, actor, room_id, start):
        if not room_id:
            return [], start, start, []
        indoor = self.engine._indoor()
        current = self.engine._effective_room_id(actor, indoor, initialize=True)
        if not current:
            raise StateError("尚未设置生活角色的初始房间")
        path = shortest_path(indoor, current, room_id)
        if path is None:
            raise StateError(f"从当前位置无法到达目标房间：{room_id}")
        events = []
        for index, step_room in enumerate(path[1:], 1):
            events.append({"room_id": step_room, "at": start + index * 120.0})
        arrival = start + max(0, len(path) - 1) * 120.0
        return events, arrival, arrival, path

    def _room_name(self, room_id):
        return next(
            (
                item["name"]
                for item in self.engine._indoor()["rooms"]
                if item["id"] == room_id
            ),
            "",
        )

    def _activity_view(self, activity):
        if not activity:
            return None
        return {
            "id": activity["id"],
            "action_id": activity["action_id"],
            "name": activity["name"],
            "step_name": activity["step_name"],
            "step_index": activity["step_index"],
            "step_count": activity["step_count"],
            "started_at": _iso(activity["started_at"]),
            "ends_at": _iso(activity["ends_at"]),
            "step_started_at": _iso(
                activity.get("step_started_at", activity["started_at"])
            ),
            "room_id": activity.get("room_id", ""),
            "room_name": activity.get("room_name", ""),
            "paused": bool(activity.get("paused")),
            "remaining_seconds": activity.get("remaining_seconds"),
        }

    def _start(self, account_id, action, timestamp):
        actor = self.canonical_actor(account_id)
        settings = self._settings(account_id)
        if settings is None or not settings["enabled"]:
            raise StateError("该QQ账号尚未启用生活内核")
        activity_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO life_cooldowns(account_id,action_id,started) VALUES (?,?,?) "
            "ON CONFLICT(account_id,action_id) DO UPDATE SET started=excluded.started",
            (account_id, action["id"], timestamp),
        )
        activity = self._begin_step(
            account_id, actor, action, 0, timestamp, activity_id
        )
        return activity

    def _begin_step(
        self,
        account_id,
        actor,
        action,
        index,
        timestamp,
        activity_id,
        *,
        original_started=None,
    ):
        if index >= len(action["steps"]):
            return None
        step = action["steps"][index]
        travel_events, arrives_at, step_started_at, path = self._travel_plan(
            actor, step["room_id"], timestamp
        )
        duration = step["duration_minutes"] * 60.0
        end_at = step_started_at + duration
        if action["finish_by_window"]:
            schedule = self._schedule()
            occurrence = self._window_occurrence(action, timestamp, schedule)
            if occurrence:
                end_at = min(end_at, occurrence[1])
        if action["id"] == "life_sleep":
            schedule = self._schedule()
            occurrence = self._window_occurrence(action, timestamp, schedule)
            if occurrence:
                end_at = min(end_at, occurrence[1])
        return {
            "id": activity_id,
            "account_id": account_id,
            "action_id": action["id"],
            "name": action["name"],
            "step_name": step["name"],
            "step_index": index + 1,
            "step_count": len(action["steps"]),
            "started_at": timestamp if original_started is None else original_started,
            "step_started_at": step_started_at,
            "arrives_at": arrives_at,
            "ends_at": max(step_started_at + 1.0, end_at),
            "room_id": step["room_id"],
            "room_name": self._room_name(step["room_id"]),
            "rates": dict(step["rates"]),
            "effects": copy.deepcopy(step["effects"]),
            "conditions": copy.deepcopy(action["conditions"]),
            "travel_events": travel_events,
            "path": path,
            "paused": False,
            "finish_by_window": action["finish_by_window"],
        }

    def _complete_step(self, account_id, activity, timestamp, definitions):
        actor = self.canonical_actor(account_id)
        owned = self.owned_state_ids()
        indexed = {state["id"]: state for state in definitions}
        _, _, values, _ = self._state_values(account_id, definitions)
        before = dict(values)
        after = dict(values)
        for effect in activity["effects"]:
            state = indexed[effect["state"]]
            value = effect["value"]
            if effect["op"] == "add":
                value = after[state["id"]] + value
                value = self.engine._bounded(state, value)
            after[state["id"]] = validate_value(state, value)
        for state_id in owned:
            if state_id in after and after[state_id] != before[state_id]:
                self._write_state(
                    state_id,
                    actor,
                    after[state_id],
                    f"生活行动：{activity['name']} / {activity['step_name']}",
                    timestamp,
                )
        after = self.engine._thresholds(
            definitions,
            actor,
            before,
            after,
            eligible_ids=owned,
            available_ids=owned,
            persist=False,
        )
        for state_id in owned:
            if state_id in after and after[state_id] != before[state_id]:
                self._write_state(
                    state_id,
                    actor,
                    after[state_id],
                    f"生活行动阈值：{activity['name']}",
                    timestamp,
                )
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"brm-life:{account_id}:{activity['action_id']}:{activity['id']}:{activity['step_index']}:{activity['ends_at']:.3f}",
            )
        )
        settings = self._settings(account_id)
        action = next(
            (
                item
                for item in self._all_actions(definitions)
                if item["id"] == activity["action_id"]
            ),
            {},
        )
        memory_status = (
            "pending"
            if settings
            and settings["enabled"]
            and settings["memory_enabled"]
            and action.get("memory_relevant")
            else "ignored"
            if settings and settings["memory_enabled"]
            else "disabled"
        )
        event = {
            "id": event_id,
            "event_id": event_id,
            "account_id": account_id,
            "occurred_at": _iso(timestamp),
            "name": activity["step_name"],
            "description": f"完成了“{activity['name']}”的第{activity['step_index']}步。",
            "kind": "activity_step",
            "status": "completed",
            "memory_status": memory_status,
            "action_id": activity["action_id"],
            "step_index": activity["step_index"],
            "room_id": activity.get("room_id", ""),
        }
        self.db.execute(
            "INSERT OR IGNORE INTO life_events(id,account_id,occurred_at,body,memory_status) VALUES (?,?,?,?,?)",
            (event_id, account_id, timestamp, self._dump(event), memory_status),
        )
        self.db.execute(
            "UPDATE life_accounts SET revision=revision+1 WHERE account_id=?",
            (account_id,),
        )

    def _write_daily_summaries(self, account_id, through, schedule):
        """Record one deterministic summary for each completed local day with activity."""
        zone = self._local_zone(schedule)
        completed_before = datetime.fromtimestamp(through, zone).date()
        cutoff = datetime.combine(
            completed_before, datetime.min.time(), zone
        ).timestamp()
        if cutoff > through:
            completed_before -= timedelta(days=1)
            cutoff = datetime.combine(
                completed_before + timedelta(days=1), datetime.min.time(), zone
            ).timestamp()

        summary_row = self.db.execute(
            'SELECT MAX(occurred_at) FROM life_events WHERE account_id=? AND body LIKE \'%"kind":"daily_summary"%\'',
            (account_id,),
        ).fetchone()
        if summary_row and summary_row[0] is not None:
            first_day = datetime.fromtimestamp(summary_row[0], zone).date()
        else:
            first_row = self.db.execute(
                'SELECT MIN(occurred_at) FROM life_events WHERE account_id=? AND body LIKE \'%"kind":"activity_step"%\' AND occurred_at<?',
                (account_id, cutoff),
            ).fetchone()
            if not first_row or first_row[0] is None:
                return
            first_day = datetime.fromtimestamp(first_row[0], zone).date()
        if first_day >= completed_before:
            return

        day_start = datetime.combine(first_day, datetime.min.time(), zone).timestamp()
        rows = self.db.execute(
            "SELECT occurred_at,body FROM life_events WHERE account_id=? AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at,id",
            (account_id, day_start, cutoff),
        ).fetchall()
        grouped = {}
        for row in rows:
            body = json.loads(row["body"])
            if body.get("kind") != "activity_step" or body.get("status") != "completed":
                continue
            day = datetime.fromtimestamp(row["occurred_at"], zone).date()
            if day >= completed_before:
                continue
            names = grouped.setdefault(day, {})
            name = str(body.get("name") or "生活活动")
            names[name] = names.get(name, 0) + 1

        settings = self._settings(account_id)
        memory_status = (
            "pending"
            if settings and settings["enabled"] and settings["memory_enabled"]
            else "disabled"
        )
        for day, counts in sorted(grouped.items()):
            activities = [
                {"name": name, "count": count} for name, count in sorted(counts.items())
            ]
            total = sum(counts.values())
            details = "、".join(
                f"{item['name']} {item['count']}次" for item in activities
            )
            event_time = datetime.combine(
                day + timedelta(days=1), datetime.min.time(), zone
            ).timestamp()
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"brm-life:daily:{account_id}:{day.isoformat()}"
                )
            )
            event = {
                "id": event_id,
                "event_id": event_id,
                "account_id": account_id,
                "occurred_at": _iso(event_time),
                "name": "生活日摘要",
                "description": f"{day.isoformat()}完成{total}项生活活动：{details}。",
                "kind": "daily_summary",
                "status": "completed",
                "memory_status": memory_status,
                "date": day.isoformat(),
                "completed_count": total,
                "activities": activities,
            }
            self.db.execute(
                "INSERT OR IGNORE INTO life_events(id,account_id,occurred_at,body,memory_status) VALUES (?,?,?,?,?)",
                (event_id, account_id, event_time, self._dump(event), memory_status),
            )

    def _activity_transition(self, account_id, activity, timestamp, definitions):
        actions = {item["id"]: item for item in self._all_actions(definitions)}
        action = actions.get(activity["action_id"])
        settings = self._settings(account_id)
        if action and not self._action_allowed(
            action, settings["preset_id"] or life_presets.PRESET_ID
        ):
            return None
        if action is None:
            return None
        if activity.get("room_id"):
            actor = self.canonical_actor(account_id)
            current_room = self.engine._effective_room_id(
                actor, self.engine._indoor(), initialize=False
            )
            if current_room != activity["room_id"]:
                self._interrupt_activity(
                    account_id,
                    activity,
                    timestamp,
                    f"完成时不在目标房间“{activity.get('room_name') or activity['room_id']}”",
                )
                return None
        self._complete_step(account_id, activity, timestamp, definitions)
        if action is None:
            return None
        next_index = activity["step_index"]
        if next_index >= len(action["steps"]):
            return None
        schedule = self._schedule(definitions)
        if activity["finish_by_window"] and not self._windows_allow(
            action, timestamp, schedule
        ):
            return None
        next_activity = self._begin_step(
            account_id,
            self.canonical_actor(account_id),
            action,
            next_index,
            timestamp,
            activity["id"],
            original_started=activity["started_at"],
        )
        return next_activity

    def _record_travel(self, account_id, activity, timestamp):
        actor = self.canonical_actor(account_id)
        room_ids = {room["id"] for room in self.engine._indoor()["rooms"]}
        for item in activity.get("travel_events", []):
            if abs(item["at"] - timestamp) < 0.001 and item["room_id"] in room_ids:
                self.db.execute(
                    "INSERT INTO indoor_locations(character_key,room_id,updated) VALUES (?,?,?) "
                    "ON CONFLICT(character_key) DO UPDATE SET room_id=excluded.room_id,updated=excluded.updated",
                    (actor["character"], item["room_id"], timestamp),
                )

    def _event_points(self, activity, start, end):
        points = []
        if activity:
            for item in activity.get("travel_events", []):
                if start < item["at"] <= end:
                    points.append(item["at"])
            arrive = activity.get("arrives_at", activity["started_at"])
            if start < arrive <= end:
                points.append(arrive)
            if start < activity["ends_at"] <= end:
                points.append(activity["ends_at"])
        return sorted(set(points))

    def _next_window_boundary(self, actions, timestamp, target, schedule):
        zone = self._local_zone(schedule)
        local = datetime.fromtimestamp(timestamp, zone)
        points = []
        for action in actions:
            if not action["enabled"]:
                continue
            for offset in range(-1, 10):
                day = local.date() + timedelta(days=offset)
                for window in action["windows"]:
                    if day.weekday() not in window["days"]:
                        continue
                    for key in ("start", "end"):
                        minute = _minutes(window[key], "窗口时间")
                        point_day = (
                            day + timedelta(days=1)
                            if key == "end"
                            and _minutes(window["end"], "窗口结束")
                            <= _minutes(window["start"], "窗口开始")
                            else day
                        )
                        point = (
                            datetime.combine(point_day, datetime.min.time(), zone)
                            + timedelta(minutes=minute)
                        ).timestamp()
                        if timestamp < point <= target:
                            points.append(point)
        points.extend(boundaries(schedule, timestamp, target))
        return min(points) if points else target

    def _settle_account(self, account_id, target, *, deterministic=False):
        settings = self._settings(account_id)
        if settings is None or not settings["enabled"]:
            return 0
        last_tick = settings["last_tick"]
        if last_tick is None:
            self.db.execute(
                "UPDATE life_accounts SET last_tick=? WHERE account_id=?",
                (target, account_id),
            )
            return 0
        cursor = min(float(last_tick), target)
        activity = json.loads(settings["activity"]) if settings["activity"] else None
        definitions = self.engine._config("states")
        schedule = self._schedule(definitions)
        actions = self._actions_for_account(account_id, self._all_actions(definitions))
        history_cutoff = target - _MODEL_DECISION_GRACE
        processed = 0
        steps = 0
        while cursor < target or (activity and activity["ends_at"] <= target):
            if steps >= _MAX_CATCHUP_EVENTS:
                break
            steps += 1
            if activity is None:
                candidates = self._candidates(account_id, cursor, definitions, actions)
                # Replay deterministic choices only for a real downtime gap.
                # The most recent 30 seconds remain available to the LLM runner.
                can_backfill = cursor < history_cutoff
                if candidates and can_backfill:
                    action = candidates[0]
                    activity = self._start(account_id, action, cursor)
                    self.db.execute(
                        "UPDATE life_accounts SET activity=? WHERE account_id=?",
                        (self._dump(activity), account_id),
                    )
                    self.db.execute(
                        "UPDATE life_accounts SET revision=revision+1 WHERE account_id=?",
                        (account_id,),
                    )
                    processed += 1
                    continue
                if cursor >= target:
                    break
                next_point = self._next_opportunity(
                    account_id, cursor, target, actions, schedule, definitions
                )
                if next_point <= cursor:
                    next_point = target
                self._integrate(account_id, cursor, next_point, None, definitions)
                cursor = next_point
                continue
            if cursor == target:
                break

            event_points = self._event_points(activity, cursor, target)
            next_event = event_points[0] if event_points else target
            # Schedule transitions split the rate calculation without changing
            # the single active activity.
            schedule_points = boundaries(schedule, cursor, next_event)
            if schedule_points:
                next_event = schedule_points[0]
            self._integrate(account_id, cursor, next_event, activity, definitions)
            self._record_travel(account_id, activity, next_event)
            cursor = next_event
            if cursor >= activity["ends_at"] - 0.001:
                activity = self._activity_transition(
                    account_id, activity, activity["ends_at"], definitions
                )
                processed += 1
                if activity is None:
                    self.db.execute(
                        "UPDATE life_accounts SET activity=NULL,revision=revision+1 WHERE account_id=?",
                        (account_id,),
                    )
                else:
                    self.db.execute(
                        "UPDATE life_accounts SET activity=? WHERE account_id=?",
                        (self._dump(activity), account_id),
                    )
        self.db.execute(
            "UPDATE life_accounts SET last_tick=?,activity=?,updated=? WHERE account_id=?",
            (
                cursor,
                self._dump(activity) if activity else None,
                self.engine.clock(),
                account_id,
            ),
        )
        self._write_daily_summaries(account_id, cursor, schedule)
        return processed

    def _interrupt_activity(self, account_id, activity, timestamp, reason):
        """Cancel an active step without applying its completion effects."""
        settings = self._settings(account_id)
        memory_status = (
            "pending"
            if settings and settings["enabled"] and settings["memory_enabled"]
            else "ignored"
            if settings and settings["memory_enabled"]
            else "disabled"
        )
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"brm-life:interrupt:{account_id}:{activity['id']}:{timestamp:.3f}",
            )
        )
        event = {
            "id": event_id,
            "event_id": event_id,
            "account_id": account_id,
            "occurred_at": _iso(timestamp),
            "name": activity.get("step_name") or activity.get("name") or "生活活动",
            "activity_name": activity.get("name") or "生活活动",
            "description": f"{reason}，已中止“{activity.get('name', '生活活动')}”的当前步骤，未结算完成效果。",
            "kind": "activity_interrupted",
            "status": "interrupted",
            "memory_status": memory_status,
            "action_id": activity.get("action_id", ""),
            "step_index": activity.get("step_index", 0),
        }
        self.db.execute(
            "INSERT OR IGNORE INTO life_events(id,account_id,occurred_at,body,memory_status) VALUES (?,?,?,?,?)",
            (event_id, account_id, timestamp, self._dump(event), memory_status),
        )
        self.db.execute(
            "UPDATE life_accounts SET activity=NULL,last_tick=?,revision=revision+1,updated=? WHERE account_id=?",
            (timestamp, timestamp, account_id),
        )
        return event

    def interrupt_for_move(self, account_id, target_room_id, timestamp):
        """Stop the current activity after a successful manual room change."""
        row = self._settings(account_id)
        if not row or not row["enabled"] or not row["activity"]:
            return None
        activity = json.loads(row["activity"])
        room = next(
            (
                item
                for item in self.engine._indoor()["rooms"]
                if item["id"] == target_room_id
            ),
            None,
        )
        target_name = room["name"] if room else target_room_id
        return self._interrupt_activity(
            account_id, activity, timestamp, f"手动移动到“{target_name}”"
        )

    def advance_account(self, account_id, now=None):
        """Settle a real chat actor's one shared life body without auto-starting."""
        account_id = self._account_id(account_id)
        now = self.engine.clock() if now is None else float(now)
        with self.engine.transaction():
            return self._settle_account(account_id, now, deterministic=True)

    def tick(self, now=None, auto_start=True):
        now = self.engine.clock() if now is None else float(now)
        if not math.isfinite(now):
            raise StateError("生活内核时间无效")
        with self.engine.transaction():
            rows = self.db.execute(
                "SELECT account_id FROM life_accounts WHERE enabled=1 ORDER BY account_id"
            ).fetchall()
            results = []
            for row in rows:
                account_id = row["account_id"]
                count = self._settle_account(
                    account_id, now, deterministic=not auto_start
                )
                activity = self._settings(account_id)["activity"]
                if auto_start and not activity:
                    candidates = self._candidates(account_id, now)
                    if candidates:
                        activity = self._start(account_id, candidates[0], now)
                        self.db.execute(
                            "UPDATE life_accounts SET activity=?,last_tick=?,revision=revision+1 WHERE account_id=?",
                            (self._dump(activity), now, account_id),
                        )
                        count += 1
                activity_data = (
                    json.loads(activity) if isinstance(activity, str) else activity
                )
                results.append(
                    {
                        "account_id": account_id,
                        "processed": count,
                        "current_activity": self._activity_view(activity_data),
                        "next_due_at": self._next_due(account_id, now),
                    }
                )
            return {"now": _iso(now), "accounts": results}

    def _next_due(self, account_id, now):
        settings = self._settings(account_id)
        if not settings or not settings["enabled"]:
            return None
        activity = json.loads(settings["activity"]) if settings["activity"] else None
        if activity:
            return _iso(activity["ends_at"])
        definitions = self.engine._config("states")
        actions = self._all_actions(definitions)
        actions = self._actions_for_account(account_id, actions)
        if self._candidates(account_id, now, definitions, actions):
            return _iso(now)
        schedule = self._schedule(definitions)
        horizon = now + 9 * 86400
        due = self._next_opportunity(
            account_id, now, horizon, actions, schedule, definitions
        )
        return _iso(due) if due is not None else None

    def decision_context(self, account_id):
        account_id = self._account_id(account_id)
        now = self.engine.clock()
        with self.engine.transaction():
            row = self._settings(account_id)
            if row is None or not row["enabled"]:
                raise StateError("该QQ账号尚未启用生活内核")
            self._settle_account(account_id, now, deterministic=True)
            row = self._settings(account_id)
            definitions = self.engine._config("states")
            actions = self._all_actions(definitions)
            actor, indexed, values, _ = self._state_values(account_id, definitions)
            rows = []
            for state_id, value in sorted(values.items()):
                state = indexed[state_id]
                rows.append(
                    {
                        "id": state_id,
                        "name": state["name"],
                        "value": value,
                        "stage": stage_for(state, value)["name"],
                    }
                )
            activity_row = self._settings(account_id)
            activity = (
                json.loads(activity_row["activity"])
                if activity_row["activity"]
                else None
            )
            schedule = self._schedule(definitions)
            candidates = (
                self._candidates(account_id, now, definitions, actions)
                if not activity
                else []
            )
            revision = self._revision(row, values, activity, actions)
            return {
                "account_id": account_id,
                "revision": revision,
                "candidates": candidates,
                "current_activity": self._activity_view(activity),
                "states": rows,
                "schedule": schedule,
                "local_time": schedule_runtime(schedule, now)["local_time"],
                "indoor": self.engine._indoor_runtime(actor, self.engine._indoor()),
            }

    @staticmethod
    def _revision(settings, values, activity, actions):
        # Natural time-rate movement is intentionally excluded: a two-second
        # runner poll must not invalidate a model request in flight.
        payload = {
            "account_revision": settings["revision"],
            "activity": activity,
            "actions": actions,
        }
        return hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()[:24]

    def start_activity(self, account_id, action_id, revision=None):
        account_id = self._account_id(account_id)
        action_id = identifier(action_id, "生活行动标识")
        now = self.engine.clock()
        with self.engine.transaction():
            settings = self._settings(account_id)
            if settings is None or not settings["enabled"]:
                raise StateError("该QQ账号尚未启用生活内核")
            if settings["activity"]:
                raise StateError("该生活角色已有正在进行的主要活动")
            definitions = self.engine._config("states")
            actions = self._all_actions(definitions)
            _, _, values, _ = self._state_values(account_id, definitions)
            if revision is not None and revision != self._revision(
                settings, values, None, actions
            ):
                raise StateError("生活决策已过期，请重新读取候选行动")
            action = next((item for item in actions if item["id"] == action_id), None)
            candidates = self._candidates(account_id, now, definitions, actions)
            if action is None or action not in candidates:
                raise StateError("该生活行动当前不满足条件、时间窗口或冷却要求")
            activity = self._start(account_id, action, now)
            self.db.execute(
                "UPDATE life_accounts SET activity=?,last_tick=?,revision=revision+1,updated=? WHERE account_id=?",
                (self._dump(activity), now, now, account_id),
            )
            return self._activity_view(activity)

    def adjust_state(self, account_id, state_id, value=None, reset=False):
        account_id = self._account_id(account_id)
        state_id = identifier(state_id, "状态标识")
        with self.engine.transaction():
            if self._settings(account_id) is None:
                raise StateError("该QQ账号尚未安装生活预设")
            if state_id not in self.owned_state_ids():
                raise StateError("只能通过QQ账号调整生活内核拥有的状态")
            definitions = self.engine._config("states")
            definition = next(
                (state for state in definitions if state["id"] == state_id), None
            )
            if definition is None:
                raise StateError("状态不存在")
            actor = self.canonical_actor(account_id)
            self.engine._row(definition, actor)
            new_value = validate_value(
                definition, definition["initial"] if reset else value
            )
            self._write_state(
                state_id,
                actor,
                new_value,
                "手动重置" if reset else "手动调整",
                self.engine.clock(),
            )
            self.db.execute(
                "UPDATE life_accounts SET revision=revision+1 WHERE account_id=?",
                (account_id,),
            )
            return self.engine._snapshot(definitions, actor)

    def pending_memories(self, limit=20):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise StateError("记忆事件读取数量应为1至500")
        with self.engine.lock:
            rows = self.db.execute(
                "SELECT e.body FROM life_events e JOIN life_accounts a ON a.account_id=e.account_id "
                "WHERE e.memory_status='pending' AND a.enabled=1 AND a.memory_enabled=1 ORDER BY e.occurred_at,e.id LIMIT ?",
                (limit,),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def mark_memory(self, event_id, status, memory_id=None, error=""):
        event_id = str(event_id or "").strip()
        if not event_id:
            raise StateError("生活事件标识不能为空")
        if status not in _MEMORY_STATUSES:
            raise StateError("未知生活记忆状态")
        memory_id = str(memory_id or "")[:256]
        error = str(error or "")[:1000]
        with self.engine.transaction():
            row = self.db.execute(
                "SELECT body FROM life_events WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise StateError("生活事件不存在")
            body = json.loads(row["body"])
            body["memory_status"] = status
            self.db.execute(
                "UPDATE life_events SET body=?,memory_status=?,memory_id=?,last_error=? WHERE id=?",
                (self._dump(body), status, memory_id, error, event_id),
            )
            return body

    def get_overview(self):
        with self.engine.lock:
            manifest = self._manifest()
            action_defs = self._all_actions() if manifest else []
            account_rows = self.db.execute(
                "SELECT id,platform_id,qq,name FROM accounts ORDER BY platform_id,qq"
            ).fetchall()
            settings_by_id = {
                row["account_id"]: row
                for row in self.db.execute("SELECT * FROM life_accounts")
            }
            accounts = []
            for item in account_rows:
                account_id = item["id"]
                row = settings_by_id.get(account_id)
                activity = (
                    json.loads(row["activity"]) if row and row["activity"] else None
                )
                pending = self.db.execute(
                    "SELECT COUNT(*) FROM life_events WHERE account_id=? AND memory_status IN ('pending','processing')",
                    (account_id,),
                ).fetchone()[0]
                last_error_row = self.db.execute(
                    "SELECT last_error FROM life_events WHERE account_id=? AND last_error<>'' ORDER BY occurred_at DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                accounts.append(
                    {
                        "id": account_id,
                        "account_id": account_id,
                        "platform_id": item["platform_id"],
                        "qq": item["qq"],
                        "name": item["name"],
                        "avatar_url": f"https://q1.qlogo.cn/g?b=qq&nk={item['qq']}&s=100",
                        "actor_id": f"life:{account_id}",
                        "persona_id": row["persona_id"] if row else "",
                        "enabled": bool(row and row["enabled"]),
                        "preset_id": row["preset_id"] if row else "",
                        "memory_enabled": bool(row and row["memory_enabled"]),
                        "model_enabled": bool(row and row["model_enabled"]),
                        "provider_id": row["provider_id"] if row else "",
                        "current_activity": self._activity_view(activity),
                        "next_due_at": self._next_due(account_id, self.engine.clock())
                        if row and row["enabled"]
                        else None,
                        "memory": {
                            "status": "pending"
                            if pending
                            else (
                                "disabled"
                                if not row or not row["memory_enabled"]
                                else "idle"
                            ),
                            "pending": pending,
                            "last_error": last_error_row[0] if last_error_row else "",
                        },
                        "context": self._latest_real_context(account_id),
                    }
                )
            events = [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM life_events ORDER BY occurred_at DESC,id DESC LIMIT 100"
                )
            ]
            neighborhood_manifest = self.engine._config("neighborhood_manifest")
            neighborhood_installed = bool(
                isinstance(neighborhood_manifest, dict)
                and neighborhood_manifest.get("preset_id")
                == neighborhood_presets.PRESET_ID
            )
            return {
                "presets": [
                    {
                        "id": neighborhood_presets.PRESET_ID,
                        "name": neighborhood_presets.PRESET_NAME,
                        "description": neighborhood_presets.PRESET_DESCRIPTION,
                        "installed": neighborhood_installed,
                        "enabled_accounts": [
                            item["id"]
                            for item in accounts
                            if item["enabled"]
                            and item["preset_id"] == neighborhood_presets.PRESET_ID
                        ],
                        "contents": neighborhood_presets.preset_contents(self.engine),
                    },
                ],
                "accounts": accounts,
                "actions": action_defs,
                "events": events,
                "options": {"personas": [], "providers": []},
            }

    def _latest_real_context(self, account_id):
        platform, qq = account_id.split(":", 1)
        for row in self.db.execute("SELECT body FROM contexts ORDER BY updated DESC"):
            try:
                actor = json.loads(row[0])
            except (TypeError, ValueError):
                continue
            identity = self._real_identity(actor)
            if identity and identity[:2] == (platform, qq):
                return actor
        return None

    @staticmethod
    def _dump(value):
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
