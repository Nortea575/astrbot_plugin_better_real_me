"""Public sharing snapshots and the isolated publisher worker."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .engine import real_actor_context, real_actor_identity
from .groups import evaluate_group, normalize_group
from .indoor import default_indoor, normalize_indoor, room_path
from .schedule import (
    _occurrences_near,
    default_schedule,
    normalize_schedule,
    schedule_runtime,
)
from .schema import StateError, stage_for, validate_value

_TABLE = "brm_public_sharing"
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
_STATE_FIELDS = ("states", "schedule", "location")
_DEFAULT_STATE: dict[str, Any] = {
    "schema": 1,
    "connected": False,
    "enabled": False,
    "account_id": "",
    "server_url": "",
    "view_url": "",
    "connection_id": "",
    "publisher_token": "",
    "sections": {"states": False, "schedule": False, "location": False},
    "last_success": None,
    "last_error": "",
    "remote_public_enabled": None,
    "pending_disable": False,
    "enable_remote_pending": False,
    "pending_settings": None,
    "remove_after_revoke": False,
    "sequence": 0,
}


class SharingHTTPError(StateError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"分享服务请求失败（HTTP {status_code}）")


def _utc_iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _normalize_sections(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict) or set(raw) - set(_STATE_FIELDS):
        raise StateError("分享栏目格式无效")
    result = {}
    for key in _STATE_FIELDS:
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise StateError("分享栏目必须使用布尔值")
        result[key] = value
    return result


def _raw_config(engine, key: str, fallback: Any) -> Any:
    row = engine.db.execute("SELECT body FROM config WHERE key=?", (key,)).fetchone()
    if row is None:
        return fallback
    try:
        return json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _account_row(engine, account_id: Any):
    if not isinstance(account_id, str) or not account_id.strip():
        raise StateError("请选择已发现的真实 QQ 账号")
    account_id = account_id.strip()
    platform, separator, qq = account_id.partition(":")
    if not separator:
        raise StateError("QQ 账号尚未由插件确认")
    try:
        platform, qq = engine._account_identity(platform, qq)
    except StateError as exc:
        raise StateError("QQ 账号尚未由插件确认") from exc
    canonical = f"{platform}:{qq}"
    row = engine.db.execute(
        "SELECT a.id,a.platform_id,a.qq,a.name,l.enabled,l.activity,l.updated,"
        "l.persona_id "
        "FROM accounts AS a LEFT JOIN life_accounts AS l ON l.account_id=a.id "
        "WHERE a.id=?",
        (canonical,),
    ).fetchone()
    if row is None or row["id"] != canonical:
        raise StateError("QQ 账号尚未由插件确认")
    return row


def _schedule_item(item: dict[str, Any] | None, category_names: dict[str, str]):
    if not item:
        return None
    return {
        "id": item["id"],
        "name": item["name"],
        "category": category_names.get(item["category"], item["category"]),
        "start_at": item["start_at"],
        "end_at": item["end_at"],
    }


def _public_location(engine, account_id: str) -> dict[str, Any]:
    raw_indoor = _raw_config(engine, "indoor", default_indoor())
    try:
        indoor = normalize_indoor(raw_indoor)
    except (StateError, TypeError, ValueError):
        indoor = normalize_indoor(default_indoor())

    actor_key = f"life:{account_id}"
    row = engine.db.execute(
        "SELECT room_id FROM indoor_locations WHERE character_key=?",
        (actor_key,),
    ).fetchone()
    room_id = row["room_id"] if row else indoor["initial_room_id"]
    rooms = {room["id"]: room for room in indoor["rooms"]}
    room = rooms.get(room_id)
    if room is None:
        room_id = ""
        room = None
    group_id = room["group_id"] if room else ""
    neighbors: set[str] = set()
    for edge in indoor["connections"]:
        if edge["a"] == room_id:
            neighbors.add(edge["b"])
        elif edge["b"] == room_id:
            neighbors.add(edge["a"])
    exits = [
        {
            "id": destination_id,
            "name": rooms[destination_id]["name"],
            "path": room_path(indoor, destination_id),
        }
        for destination_id in sorted(neighbors)
        if destination_id in rooms
    ]
    return {
        "path": room_path(indoor, room_id),
        "room_id": room_id,
        "group_id": group_id,
        "root_group_id": indoor["root_group_id"],
        "groups": [
            {
                "id": group["id"],
                "name": group["name"],
                "parent_id": group["parent_id"],
                "entry_room_id": group["entry_room_id"],
            }
            for group in indoor["groups"]
        ],
        "rooms": [
            {"id": room["id"], "name": room["name"], "group_id": room["group_id"]}
            for room in indoor["rooms"]
        ],
        "connections": [
            {"a": edge["a"], "b": edge["b"]} for edge in indoor["connections"]
        ],
        "exits": exits,
    }


class SharingExporter:
    """Build an allowlisted public view using only existing SQLite rows."""

    def __init__(self, engine):
        self.engine = engine

    def _shared_character_actor(self, account) -> dict[str, str] | None:
        persona_id = str(account["persona_id"] or "").strip()
        if not persona_id:
            return None
        for row in self.engine.db.execute(
            "SELECT body FROM contexts ORDER BY updated DESC"
        ):
            try:
                actor = real_actor_context(json.loads(row["body"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                actor = None
            identity = real_actor_identity(actor)
            if (
                identity
                and identity[:2] == (account["platform_id"], account["qq"])
                and identity[2] == persona_id
            ):
                return actor
        return None

    def build_snapshot(
        self,
        account_id: str,
        sections: dict[str, bool],
        sequence: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        sections = _normalize_sections(sections)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise StateError("分享序号无效")
        now = self.engine.clock() if now is None else float(now)
        if not math.isfinite(now):
            raise StateError("分享时间无效")

        with self.engine.lock:
            account = _account_row(self.engine, account_id)
            account_id = account["id"]
            definitions = _raw_config(self.engine, "states", [])
            if not isinstance(definitions, list):
                definitions = []
            indexed = {
                item.get("id"): item
                for item in definitions
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            actor = self.engine.life.canonical_actor(account_id)
            shared_actor = self._shared_character_actor(account)
            life_ids = self.engine.life.owned_state_ids()
            life_values = (
                self.engine.life._state_values_readonly(account_id, definitions)
                if account["persona_id"] is not None
                else {}
            )
            public_values: dict[str, Any] = {}
            state_rows: dict[str, Any] = {}
            updated_values: list[float] = []

            for definition in definitions:
                if not isinstance(definition, dict):
                    continue
                state_id = definition.get("id")
                if not isinstance(state_id, str) or not definition.get("enabled", True):
                    continue
                is_life = state_id in life_ids
                if not is_life and definition.get("scope") != "character":
                    continue
                if is_life:
                    if state_id not in life_values:
                        continue
                    value = life_values[state_id]
                    scope_key = self.engine.scope_key(definition, actor)
                else:
                    if shared_actor is None:
                        continue
                    scope_key = self.engine.scope_key(definition, shared_actor)
                    row = self.engine.db.execute(
                        "SELECT value,updated FROM runtime "
                        "WHERE state_id=? AND scope_key=?",
                        (state_id, scope_key),
                    ).fetchone()
                    # A public export never initializes a missing custom row.
                    if row is None:
                        continue
                    try:
                        value = json.loads(row["value"])
                        validate_value(definition, value)
                    except (StateError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(row["updated"], (int, float)):
                        updated_values.append(float(row["updated"]))
                public_values[state_id] = value
                state_rows[state_id] = definition

            for state_id in life_ids:
                definition = indexed.get(state_id)
                if definition is None:
                    continue
                scope_key = self.engine.scope_key(definition, actor)
                row = self.engine.db.execute(
                    "SELECT updated FROM runtime WHERE state_id=? AND scope_key=?",
                    (state_id, scope_key),
                ).fetchone()
                if row and isinstance(row["updated"], (int, float)):
                    updated_values.append(float(row["updated"]))

            categories = _raw_config(self.engine, "categories", [])
            category_names = (
                {
                    item["id"]: item["name"]
                    for item in categories
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and isinstance(item.get("name"), str)
                }
                if isinstance(categories, list)
                else {}
            )

            states = []
            if sections["states"]:
                for state_id, definition in state_rows.items():
                    value = public_values[state_id]
                    stage = stage_for(definition, value)
                    show_value = bool(definition.get("show_value", True))
                    states.append(
                        {
                            "id": state_id,
                            "name": str(definition.get("name", "")),
                            "category": category_names.get(
                                str(definition.get("category", "")),
                                str(definition.get("category", "")),
                            ),
                            "type": str(definition.get("type", "number")),
                            "value": value if show_value else None,
                            "min": definition.get("min"),
                            "max": definition.get("max"),
                            "unit": str(definition.get("unit", "")),
                            "stage": str(stage.get("name", "")),
                            "stage_description": str(stage.get("description", "")),
                            "show_value": show_value,
                            "computed": False,
                        }
                    )

                raw_groups = _raw_config(self.engine, "groups", [])
                group_ids: set[str] = set()
                if isinstance(raw_groups, list):
                    for raw_group in raw_groups:
                        if not isinstance(raw_group, dict):
                            continue
                        try:
                            group = normalize_group(raw_group, indexed)
                        except (StateError, TypeError, ValueError):
                            continue
                        group_id = group["id"]
                        if (
                            group_id in group_ids
                            or group.get("scope") != "character"
                            or not group.get("enabled", True)
                        ):
                            continue
                        factor_ids = [factor["state_id"] for factor in group["factors"]]
                        if not factor_ids or any(
                            factor_id not in public_values
                            or not state_rows[factor_id].get("show_value", True)
                            for factor_id in factor_ids
                        ):
                            continue
                        try:
                            value, _components = evaluate_group(
                                group, indexed, public_values
                            )
                        except (StateError, KeyError, TypeError, ValueError):
                            continue
                        stage = stage_for(group, value)
                        show_value = bool(group.get("show_value", True))
                        states.append(
                            {
                                "id": group_id,
                                "name": str(group.get("name", "")),
                                "category": category_names.get(
                                    str(group.get("category", "")),
                                    str(group.get("category", "")),
                                ),
                                "type": "number",
                                "value": value if show_value else None,
                                "min": group.get("min"),
                                "max": group.get("max"),
                                "unit": str(group.get("unit", "")),
                                "stage": str(stage.get("name", "")),
                                "stage_description": str(stage.get("description", "")),
                                "show_value": show_value,
                                "computed": True,
                            }
                        )
                        group_ids.add(group_id)
                states.sort(key=lambda item: (item["category"].casefold(), item["id"]))

            schedule_part = None
            activity = None
            indoor = None
            if sections["schedule"]:
                raw_schedule = _raw_config(self.engine, "schedule", default_schedule())
                try:
                    schedule = normalize_schedule(raw_schedule, definitions)
                except (StateError, TypeError, ValueError):
                    schedule = normalize_schedule(default_schedule(), definitions)
                runtime = schedule_runtime(schedule, now)
                entries = []
                seen_entries = set()
                _zone, occurrences = _occurrences_near(schedule, now)
                for start_ts, end_ts, entry in occurrences:
                    if end_ts <= now or entry["id"] in seen_entries:
                        continue
                    entries.append(
                        {
                            "id": entry["id"],
                            "name": entry["name"],
                            "category": category_names.get(
                                entry["category"], entry["category"]
                            ),
                            "start_at": datetime.fromtimestamp(
                                start_ts, ZoneInfo(schedule["timezone"])
                            ).isoformat(timespec="seconds"),
                            "end_at": datetime.fromtimestamp(
                                end_ts, ZoneInfo(schedule["timezone"])
                            ).isoformat(timespec="seconds"),
                        }
                    )
                    seen_entries.add(entry["id"])
                windows = self.engine.life.available_action_windows(account_id, now)
                schedule_part = {
                    "timezone": schedule["timezone"],
                    "local_time": runtime["local_time"],
                    "active": _schedule_item(runtime["active"], category_names),
                    "next": _schedule_item(runtime["next"], category_names),
                    "entries": entries,
                    "windows": [
                        {
                            "action_id": item["action_id"],
                            "name": item["name"],
                            "category": category_names.get(
                                item["category"], item["category"]
                            ),
                            "start_at": datetime.fromisoformat(
                                item["start_at"]
                            ).isoformat(timespec="seconds"),
                            "end_at": datetime.fromisoformat(item["end_at"]).isoformat(
                                timespec="seconds"
                            ),
                            "available_now": item["available_now"],
                        }
                        for item in windows["windows"]
                    ],
                }

            settings = self.engine.db.execute(
                "SELECT activity FROM life_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            raw_activity = settings["activity"] if settings else None
            parsed_activity = None
            if raw_activity:
                try:
                    parsed_activity = json.loads(raw_activity)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_activity = None
            if sections["schedule"] and isinstance(parsed_activity, dict):
                if sections["location"]:
                    indoor = _public_location(self.engine, account_id)
                activity_room = str(parsed_activity.get("room_id", ""))
                target_path = room_path(indoor, activity_room) if indoor else ""
                try:
                    activity = {
                        "name": str(parsed_activity.get("name", "")),
                        "step_name": str(parsed_activity.get("step_name", "")),
                        "step_index": int(parsed_activity.get("step_index", 0)),
                        "step_count": int(parsed_activity.get("step_count", 0)),
                        "started_at": _utc_iso(float(parsed_activity["started_at"])),
                        "ends_at": _utc_iso(float(parsed_activity["ends_at"])),
                        "target_path": target_path,
                    }
                except (KeyError, TypeError, ValueError, OverflowError):
                    activity = None

            location = None
            if sections["location"]:
                location = indoor or _public_location(self.engine, account_id)

            life_updated = account["updated"]
            if isinstance(life_updated, (int, float)):
                updated_values.append(float(life_updated))
            source_updated = max(updated_values) if updated_values else None
            return {
                "schema_version": 1,
                "sequence": sequence,
                "generated_at": _utc_iso(now),
                "source_updated_at": _utc_iso(source_updated)
                if source_updated is not None
                else None,
                "actor": {
                    "name": str(account["name"] or ""),
                    "avatar_url": (
                        f"https://q1.qlogo.cn/g?b=qq&nk={account['qq']}&s=100"
                    ),
                },
                "life": {
                    "enabled": bool(account["enabled"]),
                    "paused": bool(
                        not account["enabled"]
                        or (
                            isinstance(parsed_activity, dict)
                            and parsed_activity.get("paused")
                        )
                    ),
                    "activity": activity,
                },
                "states": states,
                "schedule": schedule_part,
                "location": location,
                "sections": sections,
            }


from .sharing_worker import SharingManager

__all__ = ["SharingExporter", "SharingManager"]
