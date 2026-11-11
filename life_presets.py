"""Shared home-life definitions used by the neighborhood life preset."""

from __future__ import annotations

import copy
import re

from .groups import normalize_group
from .indoor import default_indoor, normalize_indoor, shortest_path
from .schema import StateError, normalize

PRESET_ID = "neighborhood_life"
LEGACY_PRESET_ID = "home_remote"

STATE_CATEGORY_NAMES = {
    "body_needs": "身体需求",
    "mind_body_load": "身心负荷",
    "home_tasks": "居家事务",
    "work_state": "工作状态",
    "composite": "综合指标",
}
STATE_CATEGORY_IDS = {
    "body_needs": "life_body_needs",
    "mind_body_load": "life_mind_body_load",
    "home_tasks": "life_home_tasks",
    "work_state": "life_work_state",
    "composite": "life_composite",
}
STATE_CATEGORY_BY_ID = {
    "life_sleep_reserve": "body_needs",
    "life_satiety": "body_needs",
    "life_hydration": "body_needs",
    "life_toilet_need": "body_needs",
    "life_freshness": "body_needs",
    "life_sitting_fatigue": "mind_body_load",
    "life_screen_fatigue": "mind_body_load",
    "life_social_load": "mind_body_load",
    "life_mood": "mind_body_load",
    "life_home_tidy": "home_tasks",
    "life_dishes": "home_tasks",
    "life_laundry": "home_tasks",
    "life_laundry_wet": "home_tasks",
    "life_meal_stock": "home_tasks",
    "life_ingredients": "home_tasks",
    "life_delivery_timer": "home_tasks",
    "life_delivery_pending": "home_tasks",
    "life_work_pressure": "work_state",
    "life_manuscript_progress": "work_state",
    "life_task_supply": "work_state",
}
ACTION_CATEGORY_BY_ID = {
    "life_sleep": "生活起居",
    "life_eat": "生活起居",
    "life_shower": "生活起居",
    "life_wash_up": "生活起居",
    "life_toilet": "生活起居",
    "life_drink": "生活起居",
    "life_deliver_groceries": "生活起居",
    "life_work": "工作安排",
    "life_submit_manuscript": "工作安排",
    "life_accept_assignment": "工作安排",
    "life_order_groceries": "家务维护",
    "life_replenish_ingredients": "家务维护",
    "life_dishes": "家务维护",
    "life_laundry": "家务维护",
    "life_hang_laundry": "家务维护",
    "life_tidy": "家务维护",
    "life_bedtime_tidy": "家务维护",
    "life_rest": "休闲放松",
    "life_move": "休闲放松",
}
HOME_GROUP_NAME = "家"
HOME_GROUP_ID = "home"


def _state(
    state_id, name, description, initial, rate, stages, *, maximum=100, kind="number"
):
    return {
        "id": state_id,
        "name": name,
        "description": description,
        "category": "",
        "type": kind,
        "scope": "character",
        "initial": initial,
        "enabled": True,
        "inject": True,
        "show_value": True,
        "prompt": "{name}：{value}；{stage}。{description}",
        "min": 0,
        "max": maximum if kind == "number" else None,
        "rate_per_hour": rate,
        "options": [],
        "stages": [
            {"name": label, "min": low, "max": min(high, maximum), "description": text}
            for label, low, high, text in stages
            if low < maximum
        ],
        "interactions": [],
        "events": [],
    }


_STAGES = [
    ("偏低", 0, 30, "需要留意并安排恢复。"),
    ("平稳", 30, 70, "状态可以维持日常生活。"),
    ("充足", 70, 100, "储备充足，暂时没有紧迫需求。"),
]
_BAD_STAGES = [
    ("轻微", 0, 30, "当前影响较小。"),
    ("明显", 30, 70, "已经影响日常安排。"),
    ("较重", 70, 100, "需要尽快安排恢复。"),
]


def default_states(category_ids=None):
    """Return normalized, stable-ID life states without seeding any database."""
    category_ids = {**STATE_CATEGORY_IDS, **(category_ids or {})}
    raw = [
        _state(
            "life_sleep_reserve",
            "睡眠储备",
            "醒着时缓慢消耗，睡眠中恢复。",
            90,
            -2.0,
            _STAGES,
        ),
        _state("life_satiety", "饱腹感", "逐渐下降，进餐后回升。", 30, -4.6, _STAGES),
        _state("life_hydration", "水分状态", "日常饮水可补充。", 72, -3.8, _STAGES),
        _state(
            "life_toilet_need",
            "如厕需求",
            "随时间增加，如厕后降低。",
            55,
            3.2,
            [
                ("轻微", 0, 35, "暂时无需安排。"),
                ("明显", 35, 70, "适合尽快安排如厕。"),
                ("紧迫", 70, 100, "应优先安排如厕。"),
            ],
        ),
        _state(
            "life_sitting_fatigue",
            "久坐疲劳",
            "久坐增加疲劳，活动后缓解。",
            18,
            2.0,
            _BAD_STAGES,
        ),
        _state(
            "life_screen_fatigue",
            "屏幕疲劳",
            "长时间看屏幕会增加疲劳。",
            16,
            0.7,
            _BAD_STAGES,
        ),
        _state(
            "life_freshness", "清爽程度", "随时间下降，洗澡后恢复。", 60, -1.1, _STAGES
        ),
        _state(
            "life_work_pressure",
            "工作压力",
            "工作期间增加，休息和睡眠时缓解。",
            20,
            0.18,
            _BAD_STAGES,
        ),
        _state(
            "life_social_load",
            "社交负荷",
            "安静独处时逐步回落。",
            24,
            -0.35,
            _BAD_STAGES,
        ),
        _state(
            "life_mood",
            "心情愉悦度",
            "受睡眠、活动和生活整理影响。",
            68,
            -0.06,
            [
                ("低落", 0, 30, "心情偏低，适合照顾休息和日常需要。"),
                ("平静", 30, 70, "心情平稳。"),
                ("愉快", 70, 100, "心情较好。"),
            ],
        ),
        _state(
            "life_home_tidy",
            "居家整洁",
            "家务完成后提升。",
            72,
            -0.42,
            [
                ("凌乱", 0, 35, "需要安排收拾。"),
                ("尚可", 35, 70, "整体整洁度尚可。"),
                ("整洁", 70, 100, "居家环境整洁。"),
            ],
        ),
        _state(
            "life_dishes",
            "待洗餐具（件）",
            "每餐增加一件，洗碗后减少。",
            0,
            0,
            _BAD_STAGES,
        ),
        _state(
            "life_laundry",
            "待洗衣物（件）",
            "洗澡换衣后增加，洗涤后减少。",
            0,
            0,
            _BAD_STAGES,
        ),
        _state(
            "life_laundry_wet",
            "待晾衣物",
            "洗涤后需要晾晒。",
            False,
            0,
            [],
            kind="boolean",
        ),
        _state(
            "life_meal_stock",
            "可用餐份",
            "库存不足时先下单，配送到家后补充。",
            10,
            0,
            [
                ("不足", 0, 4, "需要安排补货。"),
                ("有限", 4, 10, "仍可安排几餐。"),
                ("充足", 10, 20, "餐食储备充足。"),
            ],
            maximum=20,
        ),
        _state(
            "life_ingredients",
            "食材储备",
            "做饭后下降，配送到家后补充。",
            78,
            0,
            _STAGES,
        ),
        _state(
            "life_delivery_timer",
            "配送倒计时",
            "订单送达前按小时递减。",
            0,
            -1,
            [
                ("待下单", 0, 0.01, "当前没有待配送订单。"),
                ("配送中", 0.01, 3, "订单正在配送。"),
                ("延迟", 3, 6, "配送时间较长。"),
            ],
            maximum=6,
        ),
        _state(
            "life_delivery_pending",
            "待收配送",
            "订单完成后需要在玄关收货。",
            False,
            0,
            [],
            kind="boolean",
        ),
        _state(
            "life_manuscript_progress",
            "稿件进度",
            "工作活动期间逐步推进，完成后需要明确交付。",
            8,
            0,
            [
                ("起步", 0, 25, "刚开始推进稿件。"),
                ("进行中", 25, 80, "稿件正在推进。"),
                ("接近完成", 80, 100, "稿件接近完成，可以准备交付。"),
            ],
        ),
        _state(
            "life_task_supply",
            "待写稿件",
            "完成的稿件交付后可明确接取新任务。",
            1,
            0,
            [
                ("暂无", 0, 1, "当前没有待写任务。"),
                ("有任务", 1, 5, "仍有待写任务。"),
            ],
            maximum=5,
        ),
    ]
    normalized = []
    previous = []
    for item in raw:
        result = normalize(copy.deepcopy(item), previous)
        category_key = STATE_CATEGORY_BY_ID.get(result["id"])
        if category_key:
            result["category"] = category_ids[category_key]
        normalized.append(result)
        previous.append(result)
    return normalized


def _group(group_id, name, description, factors, category_id):
    return {
        "id": group_id,
        "name": name,
        "description": description,
        "category": category_id,
        "scope": "character",
        "enabled": True,
        "inject": True,
        "show_value": True,
        "prompt": "{name}：{value}；{stage}。{description}",
        "stages": [
            {"name": "吃紧", "min": 0, "max": 35, "description": "多个方面需要照顾。"},
            {"name": "平稳", "min": 35, "max": 70, "description": "整体可以维持。"},
            {"name": "良好", "min": 70, "max": 100, "description": "整体状态较好。"},
        ],
        "factors": factors,
    }


def default_groups(category_ids=None):
    category_ids = {**STATE_CATEGORY_IDS, **(category_ids or {})}
    definitions = {state["id"]: state for state in default_states(category_ids)}
    raw = [
        _group(
            "life_energy",
            "精力",
            "睡眠和进食饮水共同影响日常精力。",
            [
                {"state_id": "life_sleep_reserve", "weight": 50, "low": 0, "high": 100},
                {"state_id": "life_satiety", "weight": 30, "low": 0, "high": 100},
                {"state_id": "life_hydration", "weight": 20, "low": 0, "high": 100},
            ],
            category_ids["composite"],
        ),
        _group(
            "life_comfort",
            "身体舒适",
            "疲劳、清爽和如厕需求共同影响身体舒适度。",
            [
                {
                    "state_id": "life_sitting_fatigue",
                    "weight": 35,
                    "low": 0,
                    "high": 100,
                    "direction": "negative",
                },
                {
                    "state_id": "life_screen_fatigue",
                    "weight": 25,
                    "low": 0,
                    "high": 100,
                    "direction": "negative",
                },
                {"state_id": "life_freshness", "weight": 20, "low": 0, "high": 100},
                {
                    "state_id": "life_toilet_need",
                    "weight": 20,
                    "low": 0,
                    "high": 100,
                    "direction": "negative",
                },
            ],
            category_ids["composite"],
        ),
        _group(
            "life_chat_reserve",
            "聊天余力",
            "睡眠、工作压力和社交负荷共同影响交流余力。",
            [
                {"state_id": "life_sleep_reserve", "weight": 40, "low": 0, "high": 100},
                {
                    "state_id": "life_work_pressure",
                    "weight": 35,
                    "low": 0,
                    "high": 100,
                    "direction": "negative",
                },
                {
                    "state_id": "life_social_load",
                    "weight": 25,
                    "low": 0,
                    "high": 100,
                    "direction": "negative",
                },
            ],
            category_ids["composite"],
        ),
    ]
    return [normalize_group(item, definitions) for item in raw]


ROOMS = [
    {
        "role": "entry",
        "id": "life_entry",
        "name": "玄关",
        "description": "居家的入口。",
        "aliases": ("玄关", "入户", "门厅"),
    },
    {
        "role": "hall",
        "id": "life_hall",
        "name": "走廊",
        "description": "连接家中各处。",
        "aliases": ("走廊", "过道"),
    },
    {
        "role": "living",
        "id": "life_living",
        "name": "客餐厅",
        "description": "休息与用餐的公共空间。",
        "aliases": ("客餐厅", "客厅", "餐厅"),
    },
    {
        "role": "kitchen",
        "id": "life_kitchen",
        "name": "厨房",
        "description": "准备餐食和清洗餐具。",
        "aliases": ("厨房", "灶间"),
    },
    {
        "role": "study",
        "id": "life_study",
        "name": "书房",
        "description": "居家工作和写稿的地方。",
        "aliases": ("书房", "工作间", "办公间"),
    },
    {
        "role": "bedroom",
        "id": "life_bedroom",
        "name": "卧室",
        "description": "睡眠和休息的地方。",
        "aliases": ("卧室", "主卧", "睡房"),
    },
    {
        "role": "bathroom",
        "id": "life_bathroom",
        "name": "卫生间",
        "description": "洗漱、如厕和洗衣。",
        "aliases": ("卫生间", "洗手间", "浴室"),
    },
    {
        "role": "balcony",
        "id": "life_balcony",
        "name": "阳台",
        "description": "短暂走动和透气的地方。",
        "aliases": ("阳台", "露台"),
    },
]


def default_actions(room_roles=None):
    """Return built-in routines; windows gate action starts in local time."""
    room_roles = (
        {item["role"]: item["id"] for item in ROOMS}
        if room_roles is None
        else room_roles
    )

    def effect(state, value, op="add"):
        return {"state": state, "op": op, "value": value}

    def condition(state, op, value):
        return {"state": state, "op": op, "value": value}

    def step(name, role, minutes, effects=(), rates=None):
        return {
            "name": name,
            "room_id": room_roles[role],
            "duration_minutes": minutes,
            "effects": list(effects),
            "rates": rates or {},
        }

    actions = [
        {
            "id": "life_sleep",
            "name": "睡眠",
            "description": "在夜间休息，睡眠储备会按实际时长恢复。",
            "enabled": True,
            "priority": 110,
            "cooldown_minutes": 900,
            "finish_by_window": True,
            "memory_relevant": True,
            "windows": [{"start": "01:00", "end": "09:00"}],
            "conditions": [condition("life_sleep_reserve", "lt", 82)],
            "steps": [
                step(
                    "夜间睡眠",
                    "bedroom",
                    480,
                    [effect("life_mood", 2)],
                    {
                        "life_sleep_reserve": 7.5,
                        "life_sitting_fatigue": -2.4,
                        "life_screen_fatigue": -1.1,
                        "life_work_pressure": -1.4,
                    },
                )
            ],
        },
        {
            "id": "life_eat",
            "name": "进餐",
            "description": "根据饱腹感和库存准备一餐。",
            "enabled": True,
            "priority": 100,
            "cooldown_minutes": 180,
            "memory_relevant": True,
            "windows": [
                {"start": "06:30", "end": "10:00"},
                {"start": "11:00", "end": "14:00"},
                {"start": "17:00", "end": "22:00"},
            ],
            "conditions": [
                condition("life_satiety", "lt", 64),
                condition("life_meal_stock", "gt", 0),
                condition("life_ingredients", "gt", 4),
            ],
            "steps": [
                step(
                    "准备并用餐",
                    "kitchen",
                    28,
                    [
                        effect("life_satiety", 40),
                        effect("life_hydration", -2),
                        effect("life_meal_stock", -1),
                        effect("life_ingredients", -8),
                        effect("life_dishes", 1),
                        effect("life_mood", 3),
                    ],
                )
            ],
        },
        {
            "id": "life_shower",
            "name": "洗澡",
            "description": "清爽程度降低时安排洗澡。",
            "enabled": True,
            "priority": 84,
            "cooldown_minutes": 1200,
            "windows": [
                {"start": "06:00", "end": "09:00"},
                {"start": "20:00", "end": "23:30"},
            ],
            "memory_relevant": True,
            "conditions": [condition("life_freshness", "lt", 70)],
            "steps": [
                step(
                    "洗澡",
                    "bathroom",
                    25,
                    [
                        effect("life_freshness", 30),
                        effect("life_laundry", 1),
                        effect("life_mood", 2),
                    ],
                )
            ],
        },
        {
            "id": "life_wash_up",
            "name": "睡前洗漱",
            "description": "睡前洗漱后再进入休息时间。",
            "enabled": True,
            "priority": 112,
            "cooldown_minutes": 1320,
            "windows": [{"start": "00:00", "end": "01:00"}],
            "conditions": [condition("life_sleep_reserve", "lt", 82)],
            "memory_relevant": False,
            "steps": [
                step(
                    "睡前洗漱",
                    "bathroom",
                    8,
                    [effect("life_freshness", 6), effect("life_mood", 1)],
                )
            ],
        },
        {
            "id": "life_work",
            "name": "居家工作",
            "description": "在工作时间以短时段推进稿件，中间重新评估生活需求。",
            "enabled": True,
            "priority": 60,
            "cooldown_minutes": 65,
            "finish_by_window": True,
            "windows": [
                {"start": "10:00", "end": "12:30", "days": [0, 1, 2, 3, 4]},
                {"start": "14:00", "end": "17:30", "days": [0, 1, 2, 3, 4]},
            ],
            "conditions": [
                condition("life_work_pressure", "lt", 82),
                condition("life_manuscript_progress", "lt", 100),
                condition("life_task_supply", "gt", 0),
            ],
            "memory_relevant": False,
            "steps": [
                step(
                    "写稿一小时",
                    "study",
                    60,
                    [],
                    {
                        "life_work_pressure": 1.6,
                        "life_manuscript_progress": 5.0,
                        "life_screen_fatigue": 1.6,
                        "life_sitting_fatigue": 1.0,
                        "life_hydration": -4.0,
                        "life_satiety": -3.2,
                    },
                )
            ],
        },
        {
            "id": "life_deliver_groceries",
            "name": "接收配送",
            "description": "配送到达后到玄关收货并补充餐食和食材。",
            "enabled": True,
            "priority": 118,
            "cooldown_minutes": 0,
            "windows": [],
            "memory_relevant": True,
            "conditions": [
                condition("life_delivery_pending", "eq", True),
                condition("life_delivery_timer", "lte", 0),
            ],
            "steps": [
                step(
                    "接收食材配送",
                    "entry",
                    10,
                    [
                        effect("life_delivery_pending", False, "set"),
                        effect("life_delivery_timer", 0, "set"),
                        effect("life_meal_stock", 12),
                        effect("life_ingredients", 70),
                        effect("life_mood", 2),
                    ],
                )
            ],
        },
        {
            "id": "life_toilet",
            "name": "如厕",
            "description": "按身体需求安排如厕。",
            "enabled": True,
            "priority": 130,
            "cooldown_minutes": 20,
            "windows": [],
            "memory_relevant": False,
            "conditions": [condition("life_toilet_need", "gte", 74)],
            "steps": [
                step(
                    "如厕",
                    "bathroom",
                    8,
                    [effect("life_toilet_need", -82), effect("life_mood", 1)],
                )
            ],
        },
        {
            "id": "life_drink",
            "name": "喝水",
            "description": "口渴时从自来水补充水分。",
            "enabled": True,
            "priority": 125,
            "cooldown_minutes": 40,
            "windows": [],
            "memory_relevant": False,
            "conditions": [condition("life_hydration", "lt", 58)],
            "steps": [step("喝水", "kitchen", 3, [effect("life_hydration", 28)])],
        },
        {
            "id": "life_order_groceries",
            "name": "订购食材",
            "description": "餐食份数较少时下单，等待配送期间可以继续其他活动。",
            "enabled": True,
            "priority": 90,
            "cooldown_minutes": 0,
            "windows": [],
            "memory_relevant": True,
            "conditions": [
                condition("life_meal_stock", "lte", 2),
                condition("life_delivery_pending", "eq", False),
            ],
            "steps": [
                step(
                    "在线订购食材",
                    "living",
                    5,
                    [
                        effect("life_delivery_pending", True, "set"),
                        effect("life_delivery_timer", 2, "set"),
                    ],
                )
            ],
        },
        {
            "id": "life_replenish_ingredients",
            "name": "补充食材",
            "description": "食材库存不足且没有在途配送时下单，避免已有餐食份数较多但无法继续进餐。",
            "enabled": True,
            "priority": 89,
            "cooldown_minutes": 0,
            "windows": [],
            "memory_relevant": True,
            "conditions": [
                condition("life_ingredients", "lte", 4),
                condition("life_delivery_pending", "eq", False),
            ],
            "steps": [
                step(
                    "补充食材并等待配送",
                    "living",
                    5,
                    [
                        effect("life_delivery_pending", True, "set"),
                        effect("life_delivery_timer", 2, "set"),
                    ],
                )
            ],
        },
        {
            "id": "life_submit_manuscript",
            "name": "交付稿件",
            "description": "稿件完成后明确交付并记录完成。",
            "enabled": True,
            "priority": 88,
            "cooldown_minutes": 0,
            "windows": [{"start": "09:00", "end": "20:00", "days": [0, 1, 2, 3, 4, 5]}],
            "memory_relevant": True,
            "conditions": [
                condition("life_manuscript_progress", "gte", 100),
                condition("life_task_supply", "gt", 0),
            ],
            "steps": [
                step(
                    "交付完成稿件",
                    "study",
                    10,
                    [
                        effect("life_manuscript_progress", 0, "set"),
                        effect("life_task_supply", -1),
                        effect("life_mood", 6),
                    ],
                )
            ],
        },
        {
            "id": "life_accept_assignment",
            "name": "接取新稿件",
            "description": "没有待写稿件时明确接取一个新任务。",
            "enabled": True,
            "priority": 42,
            "cooldown_minutes": 60,
            "windows": [{"start": "09:00", "end": "20:00", "days": [0, 1, 2, 3, 4, 5]}],
            "memory_relevant": True,
            "conditions": [condition("life_task_supply", "lte", 0)],
            "steps": [
                step(
                    "接取一份新稿件",
                    "study",
                    8,
                    [effect("life_task_supply", 1), effect("life_mood", 2)],
                )
            ],
        },
        {
            "id": "life_dishes",
            "name": "洗碗",
            "description": "餐具积累后清洗。",
            "enabled": True,
            "priority": 58,
            "cooldown_minutes": 120,
            "windows": [{"start": "09:00", "end": "22:00"}],
            "memory_relevant": False,
            "conditions": [condition("life_dishes", "gte", 5)],
            "steps": [
                step(
                    "清洗餐具",
                    "kitchen",
                    30,
                    [effect("life_dishes", -5), effect("life_home_tidy", 5)],
                )
            ],
        },
        {
            "id": "life_laundry",
            "name": "洗衣",
            "description": "衣物积累后清洗，之后单独安排晾晒。",
            "enabled": True,
            "priority": 52,
            "cooldown_minutes": 240,
            "windows": [{"start": "09:00", "end": "21:30"}],
            "memory_relevant": False,
            "conditions": [condition("life_laundry", "gte", 3)],
            "steps": [
                step(
                    "清洗衣物",
                    "bathroom",
                    40,
                    [
                        effect("life_laundry", -3),
                        effect("life_laundry_wet", True, "set"),
                    ],
                )
            ],
        },
        {
            "id": "life_hang_laundry",
            "name": "晾衣",
            "description": "洗涤完成后将衣物晾到阳台。",
            "enabled": True,
            "priority": 70,
            "cooldown_minutes": 0,
            "windows": [{"start": "08:00", "end": "22:00"}],
            "memory_relevant": False,
            "conditions": [condition("life_laundry_wet", "eq", True)],
            "steps": [
                step(
                    "晾晒衣物",
                    "balcony",
                    10,
                    [
                        effect("life_laundry_wet", False, "set"),
                        effect("life_home_tidy", 2),
                    ],
                )
            ],
        },
        {
            "id": "life_tidy",
            "name": "整理居家",
            "description": "整洁程度降低时做一轮家务。",
            "enabled": True,
            "priority": 48,
            "cooldown_minutes": 240,
            "windows": [{"start": "09:00", "end": "20:00"}],
            "memory_relevant": False,
            "conditions": [condition("life_home_tidy", "lt", 75)],
            "steps": [
                step(
                    "整理客餐厅",
                    "living",
                    45,
                    [effect("life_home_tidy", 31), effect("life_mood", 2)],
                )
            ],
        },
        {
            "id": "life_rest",
            "name": "晚间休息娱乐",
            "description": "离开屏幕休息一段时间，缓解疲劳并改善心情。",
            "enabled": True,
            "priority": 76,
            "cooldown_minutes": 1320,
            "windows": [
                {"start": "12:30", "end": "14:00"},
                {"start": "20:00", "end": "23:30"},
            ],
            "memory_relevant": False,
            "conditions": [condition("life_screen_fatigue", "gte", 30)],
            "steps": [
                step(
                    "离屏休息娱乐",
                    "living",
                    35,
                    [
                        effect("life_work_pressure", -12),
                        effect("life_screen_fatigue", -16),
                        effect("life_mood", 9),
                    ],
                )
            ],
        },
        {
            "id": "life_move",
            "name": "走动透气",
            "description": "久坐或屏幕疲劳时到阳台短暂走动。",
            "enabled": True,
            "priority": 72,
            "cooldown_minutes": 180,
            "windows": [{"start": "08:00", "end": "21:30"}],
            "memory_relevant": False,
            "conditions": [condition("life_sitting_fatigue", "gte", 44)],
            "steps": [
                step(
                    "走动透气",
                    "balcony",
                    12,
                    [
                        effect("life_sitting_fatigue", -22),
                        effect("life_screen_fatigue", -6),
                        effect("life_mood", 5),
                    ],
                )
            ],
        },
        {
            "id": "life_bedtime_tidy",
            "name": "睡前整理",
            "description": "睡前简单收拾卧室和公共空间。",
            "enabled": True,
            "priority": 55,
            "cooldown_minutes": 1410,
            "finish_by_window": True,
            "windows": [{"start": "22:45", "end": "23:30"}],
            "memory_relevant": False,
            "conditions": [],
            "steps": [
                step(
                    "睡前简单整理",
                    "bedroom",
                    12,
                    [effect("life_home_tidy", 7), effect("life_mood", 1)],
                )
            ],
        },
    ]
    for action in actions:
        action["category"] = ACTION_CATEGORY_BY_ID[action["id"]]
    return actions


def _room_key(name):
    return re.sub(r"[\s\u3000]+", "", str(name or "")).casefold()


def _unique_room_id(base, used):
    if base not in used:
        return base
    suffix = 2
    while f"{base}_{suffix}" in used:
        suffix += 1
    return f"{base}_{suffix}"


def _merge_state_categories(raw_categories):
    """Add preset categories once, reusing matching user categories by name."""
    if not isinstance(raw_categories, list):
        raise StateError("状态分类配置格式错误")
    categories = copy.deepcopy(raw_categories)
    category_ids = {}
    used_ids = {item.get("id") for item in categories if isinstance(item, dict)}
    for key, name in STATE_CATEGORY_NAMES.items():
        existing = next(
            (
                item
                for item in categories
                if isinstance(item, dict)
                and item.get("name") == name
                and isinstance(item.get("id"), str)
            ),
            None,
        )
        if existing:
            category_ids[key] = existing["id"]
            continue
        base_id = STATE_CATEGORY_IDS[key]
        category_id = base_id
        suffix = 2
        while category_id in used_ids:
            category_id = f"{base_id}_{suffix}"
            suffix += 1
        categories.append({"id": category_id, "name": name})
        used_ids.add(category_id)
        category_ids[key] = category_id
    return categories, category_ids


def _unique_group_id(base, used):
    if base not in used:
        return base
    suffix = 2
    while f"{base}_{suffix}" in used:
        suffix += 1
    return f"{base}_{suffix}"


def _ensure_home_group(indoor):
    """Find or add the root-level home group without moving existing rooms."""
    groups = list(indoor.get("groups", []))
    root_group_id = indoor.get("root_group_id", "")
    if not root_group_id or not any(
        group.get("id") == root_group_id for group in groups
    ):
        raise StateError("环境根街区配置无效")
    home = next(
        (
            group
            for group in groups
            if group.get("parent_id") == root_group_id
            and group.get("name") == HOME_GROUP_NAME
        ),
        None,
    )
    if home:
        return indoor, home["id"], False
    room_ids = {room["id"] for room in indoor.get("rooms", [])}
    group_ids = {group["id"] for group in groups}
    group_id = _unique_group_id(HOME_GROUP_ID, group_ids | room_ids)
    groups.append(
        {
            "id": group_id,
            "name": HOME_GROUP_NAME,
            "description": "居住空间。",
            "parent_id": root_group_id,
            "entry_room_id": "",
        }
    )
    updated = normalize_indoor({**indoor, "groups": groups})
    return updated, group_id, True


def _descendant_group_ids(indoor, ancestor_id):
    groups = {group["id"]: group for group in indoor.get("groups", [])}
    descendants = {ancestor_id}
    changed = True
    while changed:
        changed = False
        for group in groups.values():
            if group.get("parent_id") in descendants and group["id"] not in descendants:
                descendants.add(group["id"])
                changed = True
    return descendants


def _set_home_entry(indoor, home_group_id, room_id, *, force=False):
    """Set the home entry when its room belongs to the home subtree."""
    if not room_id or room_id not in {room["id"] for room in indoor["rooms"]}:
        return indoor
    home_groups = _descendant_group_ids(indoor, home_group_id)
    room = next(room for room in indoor["rooms"] if room["id"] == room_id)
    if room.get("group_id") not in home_groups:
        return indoor
    groups = []
    changed = False
    for group in indoor["groups"]:
        if group["id"] == home_group_id and (force or not group.get("entry_room_id")):
            groups.append({**group, "entry_room_id": room_id})
            changed = True
        else:
            groups.append(group)
    return normalize_indoor({**indoor, "groups": groups}) if changed else indoor


def install(engine):
    """Merge shared home-life definitions once, retaining user edits."""
    manifest = engine._config("life_manifest")
    if manifest:
        migrate_installed_preset(engine)
        return engine._config("life_manifest")

    raw_states = engine._config("states")
    raw_groups = engine._config("groups")
    raw_actions = engine._config("life_actions")
    categories, category_ids = _merge_state_categories(engine._config("categories"))
    preset_states = default_states(category_ids)
    preset_groups = default_groups(category_ids)
    state_ids = [item["id"] for item in preset_states]
    group_ids = [item["id"] for item in preset_groups]
    preset_actions = default_actions()
    raw_action_ids = {item.get("id") for item in raw_actions if isinstance(item, dict)}
    recovery_action = next(
        item for item in preset_actions if item["id"] == "life_replenish_ingredients"
    )
    if recovery_action["id"] in raw_action_ids:
        reserved = (
            raw_action_ids
            | set(state_ids)
            | set(group_ids)
            | {item["id"] for item in preset_actions}
        )
        recovery_action["id"] = _unique_room_id(recovery_action["id"], reserved)
    action_ids = [item["id"] for item in preset_actions]
    occupied = {item["id"] for item in raw_states}
    occupied.update(item["id"] for item in raw_groups)
    occupied.update(item["id"] for item in raw_actions)
    for item_id in state_ids + group_ids + action_ids:
        if item_id in occupied:
            raise StateError(f"生活预设标识与已有内容冲突：{item_id}")
    engine._save_config("categories", categories)

    states = list(raw_states)
    for item in preset_states:
        states.append(normalize(copy.deepcopy(item), states))

    groups = list(raw_groups)
    indexed = {item["id"]: item for item in states}
    for item in preset_groups:
        groups.append(normalize_group(item, indexed))
    engine._groups(states)

    indoor = engine._indoor()
    indoor, home_group_id, _ = _ensure_home_group(indoor)
    rooms = list(indoor["rooms"])
    connections = list(indoor["connections"])
    room_ids = {room["id"] for room in rooms}
    room_ids.update(group["id"] for group in indoor.get("groups", []))
    alias_to_role = {}
    for spec in ROOMS:
        for alias in spec["aliases"]:
            alias_to_role[_room_key(alias)] = spec["role"]
    home_groups = _descendant_group_ids(indoor, home_group_id)
    room_roles = {}
    for room in rooms:
        if room.get("group_id") not in home_groups:
            continue
        role = alias_to_role.get(_room_key(room["name"]))
        if role and role not in room_roles:
            room_roles[role] = room["id"]

    created_room_ids = []
    for spec in ROOMS:
        if spec["role"] in room_roles:
            continue
        room_id = _unique_room_id(spec["id"], room_ids)
        room_ids.add(room_id)
        room_roles[spec["role"]] = room_id
        rooms.append(
            {
                "id": room_id,
                "name": spec["name"],
                "description": spec["description"],
                "group_id": home_group_id,
            }
        )
        created_room_ids.append(room_id)

    initial_room = indoor["initial_room_id"] or room_roles["bedroom"]
    updated = normalize_indoor(
        {
            **indoor,
            "rooms": rooms,
            "connections": connections,
            "initial_room_id": initial_room,
        }
    )
    created_connections = []

    def add_connection(left, right):
        nonlocal updated
        if left == right or shortest_path(updated, left, right) is not None:
            return
        edge = {"a": min(left, right), "b": max(left, right)}
        if edge not in updated["connections"]:
            updated = normalize_indoor(
                {
                    **updated,
                    "connections": updated["connections"] + [edge],
                }
            )
            created_connections.append(edge)

    default_edges = [
        ("entry", "hall"),
        ("hall", "living"),
        ("hall", "study"),
        ("hall", "bedroom"),
        ("hall", "bathroom"),
        ("living", "kitchen"),
        ("living", "balcony"),
    ]
    for left_role, right_role in default_edges:
        add_connection(room_roles[left_role], room_roles[right_role])

    # Keep the existing initial room and all of its edges. If the current home
    # graph is detached from the life rooms, add only one access connection.
    if not any(
        shortest_path(updated, initial_room, room_id) is not None
        for room_id in room_roles.values()
    ):
        add_connection(initial_room, room_roles["entry"])

    actions = list(raw_actions)
    home_actions = default_actions(room_roles)
    for action in home_actions:
        if action["id"] == "life_replenish_ingredients":
            action["id"] = recovery_action["id"]
    actions.extend(home_actions)
    updated = _set_home_entry(updated, home_group_id, room_roles["entry"])
    engine._save_config("states", states)
    engine._save_config("groups", groups)
    engine._save_config("indoor", updated)
    engine._save_config("life_actions", actions)
    manifest = {
        "preset_id": PRESET_ID,
        "state_ids": state_ids,
        "group_ids": group_ids,
        "action_ids": action_ids,
        "ingredient_replenish_action_id": recovery_action["id"],
        "ingredient_replenish_migration_v1": True,
        "room_roles": room_roles,
        "home_group_id": home_group_id,
        "created_room_ids": created_room_ids,
        "created_connections": created_connections,
        "category_ids": category_ids,
        "category_migration_v1": True,
        "home_group_migration_v1": True,
    }
    engine._save_config("life_manifest", manifest)
    return manifest


def migrate_ingredient_replenish_action(engine):
    """Add one shared pantry fallback to an existing life core once."""
    manifest = engine._config("life_manifest")
    if (
        not isinstance(manifest, dict)
        or not manifest
        or manifest.get("ingredient_replenish_migration_v1")
    ):
        return False
    actions = engine._config("life_actions")
    if not isinstance(actions, list):
        raise StateError("生活行动配置格式错误")
    order_action = next(
        (
            item
            for item in actions
            if isinstance(item, dict) and item.get("id") == "life_order_groceries"
        ),
        None,
    )
    recovery = next(
        item
        for item in default_actions(manifest.get("room_roles", {}))
        if item["id"] == "life_replenish_ingredients"
    )
    recovery["enabled"] = bool(order_action and order_action.get("enabled", True))
    occupied = {item.get("id") for item in actions if isinstance(item, dict)}
    if recovery["id"] in occupied:
        recovery["id"] = _unique_room_id(recovery["id"], occupied)
    actions.append(recovery)
    manifest.setdefault("action_ids", []).append(recovery["id"])
    manifest["ingredient_replenish_action_id"] = recovery["id"]
    manifest["ingredient_replenish_migration_v1"] = True
    engine._save_config("life_actions", actions)
    engine._save_config("life_manifest", manifest)
    return True


def migrate_installed_preset(engine):
    """Migrate preset-owned classifications and home scope exactly once."""
    manifest = engine._config("life_manifest")
    if not isinstance(manifest, dict) or not manifest:
        return None

    changed = False
    categories = engine._config("categories")
    category_ids = manifest.get("category_ids")
    if not isinstance(category_ids, dict) or any(
        key not in category_ids or not isinstance(category_ids[key], str)
        for key in STATE_CATEGORY_NAMES
    ):
        categories, category_ids = _merge_state_categories(categories)
        engine._save_config("categories", categories)
        manifest["category_ids"] = category_ids
        changed = True

    if not manifest.get("category_migration_v1"):
        state_ids = set(manifest.get("state_ids", []))
        group_ids = set(manifest.get("group_ids", []))
        action_ids = set(manifest.get("action_ids", []))
        state_categories = engine._config("states")
        state_changed = False
        for state in state_categories:
            category_key = STATE_CATEGORY_BY_ID.get(state.get("id"))
            if (
                state.get("id") in state_ids
                and category_key
                and not str(state.get("category") or "").strip()
            ):
                state["category"] = category_ids[category_key]
                state_changed = True
        if state_changed:
            engine._save_config("states", state_categories)

        groups = engine._config("groups")
        group_changed = False
        for group in groups:
            if (
                group.get("id") in group_ids
                and not str(group.get("category") or "").strip()
            ):
                group["category"] = category_ids["composite"]
                group_changed = True
        if group_changed:
            engine._save_config("groups", groups)

        actions = engine._config("life_actions")
        action_changed = False
        for action in actions:
            default_category = ACTION_CATEGORY_BY_ID.get(action.get("id"))
            if (
                action.get("id") in action_ids
                and default_category
                and not str(action.get("category") or "").strip()
            ):
                action["category"] = default_category
                action_changed = True
        if action_changed:
            engine._save_config("life_actions", actions)
        manifest["category_migration_v1"] = True
        changed = True

    if not manifest.get("home_group_id"):
        indoor = engine._indoor()
        indoor, home_group_id, created = _ensure_home_group(indoor)
        if created:
            engine._save_config("indoor", indoor)
        manifest["home_group_id"] = home_group_id
        manifest["home_group_migration_v1"] = True
        roles = manifest.get("room_roles", {})
        if roles.get("entry"):
            updated = _set_home_entry(indoor, home_group_id, roles["entry"])
            if updated != indoor:
                engine._save_config("indoor", updated)
        changed = True

    if changed:
        engine._save_config("life_manifest", manifest)
    migrate_ingredient_replenish_action(engine)
    return engine._config("life_manifest")


def preset_contents(engine=None):
    """Return shared home-life components for a read-only street preview."""
    category_ids = dict(STATE_CATEGORY_IDS)
    categories = [
        {"id": category_ids[key], "name": name}
        for key, name in STATE_CATEGORY_NAMES.items()
    ]
    states = default_states(category_ids)
    groups = default_groups(category_ids)
    roles = {item["role"]: item["id"] for item in ROOMS}
    actions = default_actions(roles)
    rooms = [
        {key: item[key] for key in ("id", "name", "description")} for item in ROOMS
    ]
    indoor = engine._indoor() if engine is not None else default_indoor()
    root_id = indoor["root_group_id"]
    home_group = next(
        (
            item
            for item in indoor["groups"]
            if item["parent_id"] == root_id and item["name"] == HOME_GROUP_NAME
        ),
        None,
    )
    home_group_id = home_group["id"] if home_group else HOME_GROUP_ID
    room_groups = []
    connections = []
    manifest = engine._config("life_manifest") if engine is not None else {}
    if manifest:
        configured_categories = {
            item["id"]: item
            for item in engine._config("categories")
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        roles = manifest.get("room_roles", roles)
        indexed_states = {item["id"]: item for item in engine._config("states")}
        states = [
            indexed_states[item_id]
            for item_id in manifest.get("state_ids", [])
            if item_id in indexed_states
        ]
        indexed_groups = {item["id"]: item for item in engine._config("groups")}
        groups = [
            indexed_groups[item_id]
            for item_id in manifest.get("group_ids", [])
            if item_id in indexed_groups
        ]
        indexed_actions = {
            item.get("id"): item
            for item in engine._config("life_actions")
            if isinstance(item, dict)
        }
        actions = [
            copy.deepcopy(indexed_actions[item_id])
            for item_id in manifest.get("action_ids", [])
            if item_id in indexed_actions
        ]
        home_group_id = manifest.get("home_group_id", home_group_id)
        home_groups = _descendant_group_ids(indoor, home_group_id)
        rooms = [
            copy.deepcopy(room)
            for room in indoor["rooms"]
            if room["group_id"] in home_groups
        ]
        room_groups = [
            copy.deepcopy(group)
            for group in indoor["groups"]
            if group["id"] == root_id or group["id"] in home_groups
        ]
        room_ids = {room["id"] for room in rooms}
        connections = [
            copy.deepcopy(edge)
            for edge in indoor["connections"]
            if edge["a"] in room_ids and edge["b"] in room_ids
        ]
        used_category_ids = {
            item.get("category")
            for item in states + groups
            if isinstance(item.get("category"), str) and item.get("category")
        }
        categories = [
            item
            for item in configured_categories.values()
            if item["id"] in used_category_ids
        ]
    else:
        rooms = [
            {
                **{key: item[key] for key in ("id", "name", "description")},
                "group_id": home_group_id,
            }
            for item in ROOMS
        ]
        root_group = next(
            (item for item in indoor["groups"] if item["id"] == root_id), None
        )
        if root_group:
            room_groups.append(copy.deepcopy(root_group))
        if home_group:
            room_groups.append(copy.deepcopy(home_group))
            if home_group["entry_room_id"] and home_group["entry_room_id"] not in {
                room["id"] for room in rooms
            }:
                entry_room = next(
                    (
                        room
                        for room in indoor["rooms"]
                        if room["id"] == home_group["entry_room_id"]
                    ),
                    None,
                )
                if entry_room:
                    rooms.append(copy.deepcopy(entry_room))
        else:
            room_groups.append(
                {
                    "id": home_group_id,
                    "name": HOME_GROUP_NAME,
                    "description": "居住空间。",
                    "parent_id": root_id,
                    "entry_room_id": roles["entry"],
                }
            )
        default_edges = [
            ("entry", "hall"),
            ("hall", "living"),
            ("hall", "study"),
            ("hall", "bedroom"),
            ("hall", "bathroom"),
            ("living", "kitchen"),
            ("living", "balcony"),
        ]
        connections = [
            {
                "a": min(roles[left], roles[right]),
                "b": max(roles[left], roles[right]),
            }
            for left, right in default_edges
        ]
    for action in actions:
        action.setdefault("preset_ids", [])
    schedule = [
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
    return {
        "categories": categories,
        "states": states,
        "groups": groups,
        "schedule": schedule,
        "rooms": rooms,
        "room_groups": room_groups,
        "connections": connections,
        "actions": actions,
    }
