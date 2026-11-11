"""Neighborhood life locations and outdoor routines."""

from __future__ import annotations

import copy
import re

from . import life_presets
from .indoor import normalize_indoor, shortest_path
from .schema import StateError

PRESET_ID = "neighborhood_life"
PRESET_NAME = "街区生活"
PRESET_DESCRIPTION = "在家与街区之间安排起居、工作、采购和休闲。"

GROUP_SPECS = {
    "park": {
        "id": "neighborhood_park",
        "name": "公园",
        "description": "散步和短暂休息的公共绿地。",
    },
    "market": {
        "id": "neighborhood_market",
        "name": "超市",
        "description": "购买食品和日用品的商店。",
    },
    "playground": {
        "id": "neighborhood_playground",
        "name": "游乐场",
        "description": "周末休闲的游乐空间。",
    },
    "manga": {
        "id": "neighborhood_manga_club",
        "name": "漫画社",
        "description": "阅读、创作和参加活动的地方。",
    },
}

ROOT_ROOMS = {
    "lane": ("neighborhood_residents_lane", "居民巷", "家门外通往街区的安静巷道。"),
    "junction": ("neighborhood_corner", "街角路口", "居民巷、商业街和林荫路的交会处。"),
    "commercial": (
        "neighborhood_commercial_street",
        "商业街",
        "连接街区商店和游乐场入口。",
    ),
    "boulevard": (
        "neighborhood_tree_lined_road",
        "林荫路",
        "从街角通往公园的步行道路。",
    ),
}

SCOPED_ROOMS = {
    "park": {
        "entry": ("neighborhood_park_entry", "公园入口", "公园的主要入口。"),
        "path": ("neighborhood_park_path", "步道", "公园内适合散步的步道。"),
        "lake": ("neighborhood_park_lakeside", "湖边长椅", "临湖休息的长椅。"),
        "rest": ("neighborhood_park_rest", "休息区", "公园内的休息区域。"),
        "toilet": ("neighborhood_park_toilet", "卫生间", "公园公共卫生间。"),
    },
    "market": {
        "entry": ("neighborhood_market_entry", "超市入口", "超市的主要入口。"),
        "food": ("neighborhood_market_food", "食品区", "选购食品和食材。"),
        "daily": ("neighborhood_market_daily_goods", "日用品区", "选购日常用品。"),
        "cashier": ("neighborhood_market_cashier", "收银台", "结账并整理采购物品。"),
    },
    "playground": {
        "entry": ("neighborhood_playground_entry", "游乐场入口", "游乐场的主要入口。"),
        "rides": (
            "neighborhood_playground_attractions",
            "游乐设施",
            "游乐场的游乐设施区域。",
        ),
        "rest": ("neighborhood_playground_rest", "休息区", "游乐场内的休息区域。"),
        "toilet": ("neighborhood_playground_toilet", "卫生间", "游乐场公共卫生间。"),
    },
    "manga": {
        "entry": ("neighborhood_manga_entry", "门厅", "漫画社的入口和接待空间。"),
        "reading": ("neighborhood_manga_reading", "阅览区", "阅读漫画和刊物的空间。"),
        "studio": ("neighborhood_manga_studio", "创作室", "安静创作和整理稿件的空间。"),
        "event": ("neighborhood_manga_event", "活动区", "漫画社的交流活动空间。"),
    },
}

ACTION_CATEGORIES = {
    "life_neighborhood_groceries": "家务维护",
    "life_neighborhood_walk": "休闲放松",
    "life_neighborhood_lakeside": "休闲放松",
    "life_neighborhood_manga_read": "休闲放松",
    "life_neighborhood_manga_create": "工作安排",
    "life_neighborhood_playground": "休闲放松",
}


def _key(value):
    return re.sub(r"[\s\u3000]+", "", str(value or "")).casefold()


def _unique_id(base, used):
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _descendant_groups(indoor, ancestor_id):
    groups = {group["id"]: group for group in indoor["groups"]}
    descendants = {ancestor_id}
    changed = True
    while changed:
        changed = False
        for group in groups.values():
            if group["parent_id"] in descendants and group["id"] not in descendants:
                descendants.add(group["id"])
                changed = True
    return descendants


def _find_group(groups, parent_id, name):
    name_key = _key(name)
    return next(
        (
            group
            for group in groups
            if group.get("parent_id") == parent_id
            and _key(group.get("name")) == name_key
        ),
        None,
    )


def _find_room(rooms, group_ids, name):
    name_key = _key(name)
    return next(
        (
            room
            for room in rooms
            if room.get("group_id") in group_ids and _key(room.get("name")) == name_key
        ),
        None,
    )


def _ensure_groups(indoor):
    groups = copy.deepcopy(indoor["groups"])
    used = {group["id"] for group in groups} | {room["id"] for room in indoor["rooms"]}
    role_ids = {}
    created_ids = []
    for role, spec in GROUP_SPECS.items():
        existing = _find_group(groups, indoor["root_group_id"], spec["name"])
        if existing:
            role_ids[role] = existing["id"]
            continue
        group_id = _unique_id(spec["id"], used)
        groups.append(
            {
                "id": group_id,
                "name": spec["name"],
                "description": spec["description"],
                "parent_id": indoor["root_group_id"],
                "entry_room_id": "",
            }
        )
        role_ids[role] = group_id
        created_ids.append(group_id)
    return groups, role_ids, created_ids


def _ensure_rooms(indoor, groups, group_ids, home_group_id):
    rooms = copy.deepcopy(indoor["rooms"])
    used = {group["id"] for group in groups} | {room["id"] for room in rooms}
    group_index = {group["id"]: group for group in groups}
    role_ids = {"home_entry": ""}
    created_ids = []

    # The base preset manifest carries the correct, user-scoped entry room ID.
    role_ids["home_entry"] = home_group_id[1]
    root_id = indoor["root_group_id"]
    root_scope = {root_id}

    def ensure_room(role, spec, scope_ids, target_group_id, *, existing_entry=""):
        if existing_entry:
            room = next((item for item in rooms if item["id"] == existing_entry), None)
            if room is not None:
                role_ids[role] = room["id"]
                return room["id"]
        room = _find_room(rooms, scope_ids, spec[1])
        if room:
            role_ids[role] = room["id"]
            return room["id"]
        room_id = _unique_id(spec[0], used)
        rooms.append(
            {
                "id": room_id,
                "name": spec[1],
                "description": spec[2],
                "group_id": target_group_id,
            }
        )
        role_ids[role] = room_id
        created_ids.append(room_id)
        return room_id

    for role, spec in ROOT_ROOMS.items():
        ensure_room(role, spec, root_scope, root_id)

    group_entry = {}
    for group_role, room_specs in SCOPED_ROOMS.items():
        group_id = group_ids[group_role]
        scope = _descendant_groups({"groups": groups}, group_id)
        group = group_index[group_id]
        entry_role = f"{group_role}_entry"
        entry_spec = room_specs["entry"]
        # A preexisting group entry is the user's chosen entrance, regardless of
        # its display name. Keep it and don't add a duplicate entrance room.
        entry_id = ensure_room(
            entry_role,
            entry_spec,
            scope,
            group_id,
            existing_entry=group.get("entry_room_id", ""),
        )
        group_entry[group_role] = entry_id
        for room_role, spec in room_specs.items():
            if room_role == "entry":
                continue
            ensure_room(f"{group_role}_{room_role}", spec, scope, group_id)
        if not group.get("entry_room_id"):
            group["entry_room_id"] = entry_id

    return rooms, groups, role_ids, created_ids, group_entry


def _add_connections(indoor, pairs):
    updated = indoor
    created = []
    for left, right in pairs:
        if not left or not right or left == right:
            continue
        if shortest_path(updated, left, right) is not None:
            continue
        edge = {"a": min(left, right), "b": max(left, right)}
        updated = normalize_indoor(
            {**updated, "connections": updated["connections"] + [edge]}
        )
        created.append(edge)
    return updated, created


def _action_specs(rooms):
    def effect(state, value, op="add"):
        return {"state": state, "op": op, "value": value}

    def condition(state, op, value):
        return {"state": state, "op": op, "value": value}

    outdoor_body = [
        condition("life_sleep_reserve", "gt", 35),
        condition("life_satiety", "gt", 30),
        condition("life_hydration", "gt", 40),
        condition("life_toilet_need", "lt", 60),
    ]

    def step(name, role, minutes, effects=(), rates=None):
        return {
            "name": name,
            "room_id": rooms[role],
            "duration_minutes": minutes,
            "effects": list(effects),
            "rates": rates or {},
        }

    shared = {"enabled": True, "preset_ids": [PRESET_ID], "memory_relevant": True}
    return [
        {
            **shared,
            "id": "life_neighborhood_groceries",
            "name": "街区采购",
            "description": "食品库存不足且没有在途配送时，完成食品、日用品选购和收银后补充库存。",
            "category": ACTION_CATEGORIES["life_neighborhood_groceries"],
            "priority": 95,
            "cooldown_minutes": 360,
            "windows": [{"start": "08:00", "end": "20:30"}],
            "conditions": outdoor_body
            + [
                condition("life_ingredients", "lte", 28),
                condition("life_delivery_pending", "eq", False),
            ],
            "steps": [
                step("进入超市", "market_entry", 1),
                step("挑选食品", "market_food", 12),
                step("挑选日用品", "market_daily", 8),
                step(
                    "结账并整理采购物品",
                    "market_cashier",
                    5,
                    [
                        effect("life_meal_stock", 8),
                        effect("life_ingredients", 55),
                        effect("life_mood", 1),
                    ],
                ),
                step("返家", "home_entry", 1),
            ],
        },
        {
            **shared,
            "id": "life_neighborhood_walk",
            "name": "公园散步",
            "description": "久坐并积累一定工作压力时，在下班后或周末沿公园步道散步。",
            "category": ACTION_CATEGORIES["life_neighborhood_walk"],
            "priority": 74,
            "cooldown_minutes": 180,
            "windows": [
                {"start": "17:30", "end": "20:00", "days": [0, 1, 2, 3, 4]},
                {"start": "08:00", "end": "20:00", "days": [5, 6]},
            ],
            "conditions": outdoor_body
            + [
                condition("life_sitting_fatigue", "gte", 35),
                condition("life_work_pressure", "gte", 25),
            ],
            "steps": [
                step("进入公园", "park_entry", 1),
                step(
                    "沿步道散步",
                    "park_path",
                    30,
                    [
                        effect("life_sitting_fatigue", -20),
                        effect("life_work_pressure", -4),
                        effect("life_mood", 4),
                    ],
                    {
                        "life_satiety": -5.2,
                        "life_hydration": -4.8,
                        "life_sleep_reserve": -2.8,
                        "life_sitting_fatigue": -0.8,
                        "life_work_pressure": -1.1,
                    },
                ),
                step("返家", "home_entry", 1),
            ],
        },
        {
            **shared,
            "id": "life_neighborhood_lakeside",
            "name": "湖边休息",
            "description": "工作压力较高时在湖边安静坐一会儿，再回家。",
            "category": ACTION_CATEGORIES["life_neighborhood_lakeside"],
            "priority": 78,
            "cooldown_minutes": 240,
            "windows": [
                {"start": "17:30", "end": "20:30", "days": [0, 1, 2, 3, 4]},
                {"start": "09:00", "end": "20:30", "days": [5, 6]},
            ],
            "conditions": outdoor_body + [condition("life_work_pressure", "gte", 48)],
            "steps": [
                step("进入公园", "park_entry", 1),
                step(
                    "在湖边休息",
                    "park_lake",
                    25,
                    [
                        effect("life_work_pressure", -14),
                        effect("life_social_load", -3),
                        effect("life_mood", 5),
                    ],
                    {
                        "life_sleep_reserve": -2.2,
                        "life_work_pressure": -0.7,
                        "life_screen_fatigue": -0.45,
                        "life_social_load": -0.65,
                    },
                ),
                step("返家", "home_entry", 1),
            ],
        },
        {
            **shared,
            "id": "life_neighborhood_manga_read",
            "name": "漫画社阅读",
            "description": "晚间或周末到漫画社阅读一会儿，放松后回家。",
            "category": ACTION_CATEGORIES["life_neighborhood_manga_read"],
            "priority": 73,
            "cooldown_minutes": 480,
            "windows": [
                {"start": "18:30", "end": "21:30", "days": [0, 1, 2, 3, 4]},
                {"start": "10:00", "end": "21:30", "days": [5, 6]},
            ],
            "conditions": outdoor_body + [condition("life_screen_fatigue", "gte", 24)],
            "steps": [
                step("进入漫画社", "manga_entry", 1),
                step(
                    "在阅览区阅读",
                    "manga_reading",
                    40,
                    [
                        effect("life_work_pressure", -7),
                        effect("life_social_load", -4),
                        effect("life_mood", 5),
                        effect("life_screen_fatigue", -9),
                    ],
                    {
                        "life_sleep_reserve": -2.1,
                        "life_work_pressure": -0.55,
                        "life_screen_fatigue": -0.5,
                        "life_social_load": -0.55,
                    },
                ),
                step("返家", "home_entry", 1),
            ],
        },
        {
            **shared,
            "id": "life_neighborhood_manga_create",
            "name": "漫画社创作",
            "description": "工作日到漫画社创作室继续手头稿件，并把进展记入现有工作状态。",
            "category": ACTION_CATEGORIES["life_neighborhood_manga_create"],
            "priority": 63,
            "cooldown_minutes": 180,
            "windows": [
                {"start": "10:00", "end": "12:30", "days": [0, 1, 2, 3, 4]},
                {"start": "14:00", "end": "17:00", "days": [0, 1, 2, 3, 4]},
            ],
            "conditions": outdoor_body
            + [
                condition("life_manuscript_progress", "lt", 100),
                condition("life_task_supply", "gt", 0),
                condition("life_work_pressure", "lt", 78),
            ],
            "steps": [
                step("进入漫画社", "manga_entry", 1),
                step(
                    "继续创作手头稿件",
                    "manga_studio",
                    55,
                    [effect("life_manuscript_progress", 4)],
                    {
                        "life_manuscript_progress": 2.3,
                        "life_work_pressure": 1.8,
                        "life_screen_fatigue": 1.4,
                        "life_sitting_fatigue": 2.7,
                        "life_hydration": -5.0,
                        "life_satiety": -5.3,
                        "life_sleep_reserve": -2.3,
                    },
                ),
                step("返家", "home_entry", 1),
            ],
        },
        {
            **shared,
            "id": "life_neighborhood_playground",
            "name": "游乐场休闲",
            "description": "身体储备充足时，只在周末去游乐场休闲；行动至少间隔七天。",
            "category": ACTION_CATEGORIES["life_neighborhood_playground"],
            "priority": 76,
            "cooldown_minutes": 10080,
            "windows": [{"start": "09:00", "end": "18:00", "days": [5, 6]}],
            "conditions": [
                condition("life_sleep_reserve", "gt", 60),
                condition("life_satiety", "gt", 55),
                condition("life_hydration", "gt", 55),
                condition("life_freshness", "gt", 20),
                condition("life_toilet_need", "lt", 45),
            ],
            "steps": [
                step("进入游乐场", "playground_entry", 1),
                step(
                    "体验游乐设施",
                    "playground_rides",
                    40,
                    [effect("life_mood", 7), effect("life_work_pressure", -8)],
                    {
                        "life_hydration": -5.8,
                        "life_satiety": -5.5,
                        "life_sleep_reserve": -3.0,
                        "life_freshness": -1.5,
                    },
                ),
                step(
                    "在休息区缓一缓",
                    "playground_rest",
                    12,
                    [effect("life_sitting_fatigue", -8)],
                ),
                step("返家", "home_entry", 1),
            ],
        },
    ]


def _unique_action_ids(actions):
    used = {item.get("id") for item in actions if isinstance(item, dict)}
    result = {}
    for action in _action_specs({role: role for role in _all_action_room_roles()}):
        base = action["id"]
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result[base] = candidate
    return result


def _all_action_room_roles():
    return (
        "home_entry",
        "park_entry",
        "park_path",
        "park_lake",
        "market_entry",
        "market_food",
        "market_daily",
        "market_cashier",
        "manga_entry",
        "manga_reading",
        "manga_studio",
        "playground_entry",
        "playground_rides",
        "playground_rest",
    )


def install(engine):
    """Merge street rooms and actions once, preserving preexisting user edits."""
    manifest = engine._config("neighborhood_manifest")
    if isinstance(manifest, dict) and manifest.get("preset_id") == PRESET_ID:
        return manifest

    base_manifest = life_presets.install(engine)
    indoor = engine._indoor()
    root_id = indoor["root_group_id"]
    groups, group_ids, created_group_ids = _ensure_groups(indoor)
    rooms, groups, room_roles, created_room_ids, group_entries = _ensure_rooms(
        indoor,
        groups,
        group_ids,
        (base_manifest["home_group_id"], base_manifest["room_roles"]["entry"]),
    )
    updated = normalize_indoor(
        {
            **indoor,
            "groups": groups,
            "rooms": rooms,
            "initial_room_id": indoor["initial_room_id"]
            or base_manifest["room_roles"]["bedroom"],
        }
    )
    pairs = [
        (base_manifest["room_roles"]["entry"], room_roles["lane"]),
        (room_roles["lane"], room_roles["junction"]),
        (room_roles["junction"], room_roles["commercial"]),
        (room_roles["junction"], room_roles["boulevard"]),
        (room_roles["boulevard"], group_entries["park"]),
        (room_roles["commercial"], group_entries["market"]),
        (room_roles["commercial"], group_entries["playground"]),
        (room_roles["lane"], group_entries["manga"]),
        (group_entries["park"], room_roles["park_path"]),
        (room_roles["park_path"], room_roles["park_lake"]),
        (room_roles["park_path"], room_roles["park_rest"]),
        (room_roles["park_path"], room_roles["park_toilet"]),
        (group_entries["market"], room_roles["market_food"]),
        (room_roles["market_food"], room_roles["market_daily"]),
        (room_roles["market_daily"], room_roles["market_cashier"]),
        (group_entries["playground"], room_roles["playground_rides"]),
        (room_roles["playground_rides"], room_roles["playground_rest"]),
        (room_roles["playground_rest"], room_roles["playground_toilet"]),
        (group_entries["manga"], room_roles["manga_reading"]),
        (group_entries["manga"], room_roles["manga_studio"]),
        (room_roles["manga_reading"], room_roles["manga_event"]),
        (room_roles["manga_studio"], room_roles["manga_event"]),
    ]
    updated, created_connections = _add_connections(updated, pairs)

    raw_actions = engine._config("life_actions")
    if not isinstance(raw_actions, list):
        raise StateError("生活行动配置格式错误")
    id_map = _unique_action_ids(raw_actions)
    action_rooms = dict(room_roles)
    action_rooms.update(
        {
            "park_entry": group_entries["park"],
            "market_entry": group_entries["market"],
            "playground_entry": group_entries["playground"],
            "manga_entry": group_entries["manga"],
        }
    )
    actions = _action_specs(action_rooms)
    for action in actions:
        action["id"] = id_map[action["id"]]
    action_ids = [item["id"] for item in actions]
    ownership = {
        "created_group_ids": created_group_ids,
        "created_room_ids": created_room_ids,
        "created_connection_edges": created_connections,
        "created_action_ids": action_ids,
    }
    manifest = {
        "preset_id": PRESET_ID,
        "base_preset_id": life_presets.PRESET_ID,
        "root_group_id": root_id,
        "room_group_roles": group_ids,
        "room_roles": room_roles,
        "group_entry_roles": group_entries,
        "state_ids": list(base_manifest.get("state_ids", [])),
        "group_ids": list(base_manifest.get("group_ids", [])),
        "action_ids": action_ids,
        "ownership": ownership,
        "created_group_ids": created_group_ids,
        "created_room_ids": created_room_ids,
        "created_connections": created_connections,
        "home_group_id": base_manifest["home_group_id"],
    }
    engine._save_config("indoor", updated)
    engine._save_config("life_actions", raw_actions + actions)
    engine._save_config("neighborhood_manifest", manifest)
    return manifest


def _room_groups_for(indoor, group_ids):
    groups = {item["id"]: item for item in indoor["groups"]}
    required = set(group_ids)
    for group_id in tuple(required):
        current = groups.get(group_id)
        while current and current["parent_id"]:
            required.add(current["parent_id"])
            current = groups.get(current["parent_id"])
    return [
        copy.deepcopy(group) for group in indoor["groups"] if group["id"] in required
    ]


def _schedule(actions):
    return [
        {
            "action_id": action["id"],
            "name": action["name"],
            "category": action.get("category", ""),
            "start": window["start"],
            "end": window["end"],
            "days": window.get("days", list(range(7))),
            "description": action.get("description", ""),
        }
        for action in actions
        for window in action.get("windows", [])
    ]


def preset_contents(engine=None, base_contents=None):
    """Return neighborhood specs or installed user-edited resources for the UI."""
    base = base_contents or life_presets.preset_contents(engine)
    manifest = engine._config("neighborhood_manifest") if engine is not None else {}
    if isinstance(manifest, dict) and manifest.get("preset_id") == PRESET_ID:
        indoor = engine._indoor()
        home_manifest = engine._config("life_manifest") or {}
        scoped_group_ids = {manifest.get("root_group_id", indoor["root_group_id"])}
        home_group_id = manifest.get("home_group_id") or home_manifest.get(
            "home_group_id", ""
        )
        if home_group_id:
            scoped_group_ids.update(_descendant_groups(indoor, home_group_id))
        for group_id in manifest.get("room_group_roles", {}).values():
            scoped_group_ids.update(_descendant_groups(indoor, group_id))
        room_groups = _room_groups_for(indoor, scoped_group_ids)
        scoped_room_ids = {
            room["id"]
            for room in indoor["rooms"]
            if room["group_id"] in scoped_group_ids
        }
        rooms = [
            copy.deepcopy(room)
            for room in indoor["rooms"]
            if room["id"] in scoped_room_ids
        ]
        edges = [
            copy.deepcopy(edge)
            for edge in indoor["connections"]
            if edge["a"] in scoped_room_ids and edge["b"] in scoped_room_ids
        ]
        allowed_ids = set(manifest.get("action_ids", []))
        all_actions = engine.life._all_actions()
        actions = [
            copy.deepcopy(action)
            for action in all_actions
            if (
                action.get("id") in allowed_ids
                or not action.get("preset_ids")
                or PRESET_ID in action.get("preset_ids", [])
            )
        ]
    else:
        indoor = (
            engine._indoor()
            if engine is not None
            else {"root_group_id": "district", "groups": []}
        )
        root_id = indoor.get("root_group_id", "district")
        home_group_id = next(
            (
                item["id"]
                for item in indoor.get("groups", [])
                if item.get("parent_id") == root_id and item.get("name") == "家"
            ),
            "home",
        )
        group_ids = {role: spec["id"] for role, spec in GROUP_SPECS.items()}
        role_rooms = {
            role: {
                "id": spec[0],
                "name": spec[1],
                "description": spec[2],
                "group_id": root_id,
            }
            for role, spec in ROOT_ROOMS.items()
        }
        for group_role, specs in SCOPED_ROOMS.items():
            for room_role, spec in specs.items():
                role_rooms[f"{group_role}_{room_role}"] = {
                    "id": spec[0],
                    "name": spec[1],
                    "description": spec[2],
                    "group_id": group_ids[group_role],
                }
        home_manifest = engine._config("life_manifest") if engine is not None else {}
        configured_home_roles = (
            home_manifest.get("room_roles", {})
            if isinstance(home_manifest, dict)
            else {}
        )
        if configured_home_roles:
            for home_role, room_id in configured_home_roles.items():
                home_room = next(
                    (room for room in base["rooms"] if room["id"] == room_id), None
                )
                if home_room:
                    role_rooms[f"home_{home_role}"] = {
                        **home_room,
                        "group_id": home_group_id,
                    }
        else:
            default_roles = {item["id"]: item["role"] for item in life_presets.ROOMS}
            for home_room in base["rooms"]:
                home_role = default_roles.get(home_room["id"])
                if home_role:
                    role_rooms[f"home_{home_role}"] = {
                        **home_room,
                        "group_id": home_group_id,
                    }
        roles = {key: item["id"] for key, item in role_rooms.items()}
        actions = base["actions"] + _action_specs(
            {
                **roles,
                "park_entry": ROOT_SCOPED_ENTRY_IDs(group_ids)["park"],
                "market_entry": ROOT_SCOPED_ENTRY_IDs(group_ids)["market"],
                "playground_entry": ROOT_SCOPED_ENTRY_IDs(group_ids)["playground"],
                "manga_entry": ROOT_SCOPED_ENTRY_IDs(group_ids)["manga"],
            }
        )
        rooms = [copy.deepcopy(room) for room in role_rooms.values()]
        root_group = next(
            (item for item in indoor.get("groups", []) if item["id"] == root_id), None
        )
        home_group = next(
            (item for item in indoor.get("groups", []) if item["id"] == home_group_id),
            None,
        )
        room_groups = [copy.deepcopy(item) for item in (root_group, home_group) if item]
        if home_group is None:
            room_groups.append(
                {
                    "id": home_group_id,
                    "name": "家",
                    "description": "居住空间。",
                    "parent_id": root_id,
                    "entry_room_id": roles.get("home_entry", "life_entry"),
                }
            )
        room_groups.extend(
            {
                "id": group_ids[role],
                "name": spec["name"],
                "description": spec["description"],
                "parent_id": root_id,
                "entry_room_id": SCOPED_ROOMS[role]["entry"][0],
            }
            for role, spec in GROUP_SPECS.items()
        )
        edges = []
        home_entry_id = roles.get("home_entry", "life_entry")
        for left, right in [
            (home_entry_id, roles["lane"]),
            (roles["lane"], roles["junction"]),
            (roles["junction"], roles["commercial"]),
            (roles["junction"], roles["boulevard"]),
            (roles["boulevard"], SCOPED_ROOMS["park"]["entry"][0]),
            (roles["commercial"], SCOPED_ROOMS["market"]["entry"][0]),
            (roles["commercial"], SCOPED_ROOMS["playground"]["entry"][0]),
            (roles["lane"], SCOPED_ROOMS["manga"]["entry"][0]),
        ]:
            edges.append({"a": min(left, right), "b": max(left, right)})
        for left_role, right_role in [
            ("home_entry", "home_hall"),
            ("home_hall", "home_living"),
            ("home_hall", "home_study"),
            ("home_hall", "home_bedroom"),
            ("home_hall", "home_bathroom"),
            ("home_living", "home_kitchen"),
            ("home_living", "home_balcony"),
        ]:
            left, right = roles[left_role], roles[right_role]
            edge = {"a": min(left, right), "b": max(left, right)}
            if edge not in edges:
                edges.append(edge)
        for group_role, local_pairs in {
            "park": [
                ("entry", "path"),
                ("path", "lake"),
                ("path", "rest"),
                ("path", "toilet"),
            ],
            "market": [("entry", "food"), ("food", "daily"), ("daily", "cashier")],
            "playground": [("entry", "rides"), ("rides", "rest"), ("rest", "toilet")],
            "manga": [
                ("entry", "reading"),
                ("entry", "studio"),
                ("reading", "event"),
                ("studio", "event"),
            ],
        }.items():
            for left_role, right_role in local_pairs:
                left, right = (
                    SCOPED_ROOMS[group_role][left_role][0],
                    SCOPED_ROOMS[group_role][right_role][0],
                )
                edges.append({"a": min(left, right), "b": max(left, right)})

    for action in actions:
        action.setdefault("preset_ids", [])
    return {
        "categories": base["categories"],
        "states": base["states"],
        "groups": base["groups"],
        "rooms": rooms,
        "room_groups": room_groups,
        "connections": edges,
        "schedule": _schedule(actions),
        "actions": actions,
    }


def ROOT_SCOPED_ENTRY_IDs(group_ids):
    return {role: SCOPED_ROOMS[role]["entry"][0] for role in group_ids}
