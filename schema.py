"""Declarative state definitions shared by the view and the runtime."""

from __future__ import annotations

import itertools
import math
import re


class StateError(ValueError):
    pass


def number(value, label="数值"):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise StateError(f"{label}必须是有限数字")
    return value


def identifier(value, label="标识"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise StateError(
            f"{label}应以小写英文字母开头，仅含小写字母、数字或下划线（最多64位）"
        )
    return value


def text(value, label, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise StateError(f"请填写{label}")
    return value.strip()


def validate_value(definition, value):
    kind = definition["type"]
    if kind == "number":
        number(value)
        if definition.get("min") is not None and value < definition["min"]:
            raise StateError(f"{definition['name']}不能低于{definition['min']}")
        if definition.get("max") is not None and value > definition["max"]:
            raise StateError(f"{definition['name']}不能高于{definition['max']}")
    elif kind == "boolean" and not isinstance(value, bool):
        raise StateError("布尔状态的值必须为 true 或 false")
    elif kind == "enum" and value not in definition["options"]:
        raise StateError("枚举值必须属于已定义选项")
    elif kind == "text" and not isinstance(value, str):
        raise StateError("文本状态的值必须为字符串")
    return value


def condition(raw, definitions, self_id):
    if not isinstance(raw, dict):
        raise StateError("条件格式错误")
    state_id = raw.get("state", self_id)
    if state_id == "self":
        state_id = self_id
    if state_id not in definitions:
        raise StateError(f"条件引用了不存在的状态：{state_id}")
    op = raw.get("op", "eq")
    if op not in {"eq", "ne", "lt", "lte", "gt", "gte"}:
        raise StateError("不支持的条件运算")
    value = raw.get("value")
    target = definitions[state_id]
    if target["type"] == "number":
        number(value, "条件数值")
    else:
        validate_value(target, value)
        if op not in {"eq", "ne"}:
            raise StateError("非数值状态仅支持等于和不等于条件")
    return {"state": state_id, "op": op, "value": value}


def rule(raw, definitions, self_id, event=False):
    if not isinstance(raw, dict):
        raise StateError("交互或事件格式错误")
    result = {
        "id": identifier(raw.get("id"), "交互或事件标识"),
        "name": text(raw.get("name", ""), "交互或事件名称", True),
        "description": text(raw.get("description", ""), "用途说明"),
        "conditions": [
            condition(c, definitions, self_id) for c in raw.get("conditions", [])
        ],
        "effects": [],
    }
    if not raw.get("effects"):
        raise StateError("交互或事件至少需要一项状态变化")
    for effect in raw["effects"]:
        target_id = effect.get("state", self_id)
        if target_id == "self":
            target_id = self_id
        if target_id not in definitions:
            raise StateError(f"变化引用了不存在的状态：{target_id}")
        target = definitions[target_id]
        op = effect.get("op", "add")
        value = effect.get("value")
        if op == "add" and target["type"] == "number":
            number(value, "变化量")
        elif op == "set":
            validate_value(target, value)
        else:
            raise StateError("数值支持增加/减少和赋值，其他类型仅支持赋值")
        result["effects"].append({"state": target_id, "op": op, "value": value})
    if event:
        trigger = raw.get("trigger", "semantic")
        if trigger not in {"semantic", "threshold"}:
            raise StateError("事件类型应为对话语义或阈值事件")
        result["trigger"] = trigger
        if trigger == "threshold":
            result["when"] = condition(
                {**raw.get("when", {}), "state": self_id}, definitions, self_id
            )
    else:
        result["allow_user"] = bool(raw.get("allow_user", True))
    return result


def normalize(raw, existing):
    if not isinstance(raw, dict):
        raise StateError("状态定义必须为对象")
    state_id = identifier(raw.get("id"), "状态标识")
    kind = raw.get("type", "number")
    if kind not in {"number", "boolean", "enum", "text"}:
        raise StateError("不支持的状态类型")
    scope = raw.get("scope", "character")
    if scope not in {"character", "conversation", "user"}:
        raise StateError("不支持的状态归属")
    result = {
        "id": state_id,
        "name": text(raw.get("name", ""), "状态名称", True),
        "description": text(raw.get("description", ""), "状态含义"),
        "category": text(raw.get("category", ""), "分类"),
        "type": kind,
        "scope": scope,
        "initial": raw.get(
            "initial", 0 if kind == "number" else False if kind == "boolean" else ""
        ),
        "enabled": bool(raw.get("enabled", True)),
        "inject": bool(raw.get("inject", True)),
        "show_value": bool(raw.get("show_value", True)),
        "prompt": text(
            raw.get("prompt", "{name}：{value}；{stage}。{description}"), "描述模板"
        ),
        "min": None,
        "max": None,
        "options": [],
        "rate_per_hour": 0,
        "stages": [],
        "interactions": [],
        "events": [],
    }
    if "editor_blocks" in raw:
        blocks = raw["editor_blocks"]
        allowed_blocks = {"time", "stages", "interactions", "events"}
        if (
            not isinstance(blocks, list)
            or any(
                not isinstance(block, str) or block not in allowed_blocks
                for block in blocks
            )
            or len(blocks) != len(set(blocks))
        ):
            raise StateError(
                "积木显示顺序仅支持不重复的 time/stages/interactions/events"
            )
        result["editor_blocks"] = list(blocks)
    if kind == "number":
        for key in ["min", "max"]:
            if raw.get(key) is not None:
                result[key] = number(raw[key], key)
        if (
            result["min"] is not None
            and result["max"] is not None
            and result["min"] >= result["max"]
        ):
            raise StateError("最小值必须小于最大值")
        result["rate_per_hour"] = number(raw.get("rate_per_hour", 0), "每小时变化量")
    elif kind == "enum":
        options = raw.get("options", [])
        if (
            not isinstance(options, list)
            or not options
            or any(not isinstance(v, str) or not v for v in options)
        ):
            raise StateError("枚举至少需要一个非空文本选项")
        if len(options) != len(set(options)):
            raise StateError("枚举选项不能重复")
        result["options"] = options
    validate_value(result, result["initial"])
    for stage in raw.get("stages", []):
        entry = {
            "name": text(stage.get("name", ""), "阶段名称", True),
            "description": text(stage.get("description", ""), "阶段描述"),
        }
        if kind == "number":
            entry.update(min=stage.get("min"), max=stage.get("max"))
            for key in ["min", "max"]:
                if entry[key] is not None:
                    number(entry[key], "阶段边界")
            if (
                entry["min"] is not None
                and entry["max"] is not None
                and entry["min"] >= entry["max"]
            ):
                raise StateError("阶段下界应小于上界")
        else:
            entry["value"] = validate_value(result, stage.get("value"))
        result["stages"].append(entry)
    if kind == "number":
        ordered = sorted(
            result["stages"],
            key=lambda s: float("-inf") if s["min"] is None else s["min"],
        )
        for previous, following in itertools.pairwise(ordered):
            if (
                previous["max"] is None
                or following["min"] is None
                or previous["max"] > following["min"]
            ):
                raise StateError("阶段区间不能重叠")
    elif len({dump_stage["value"] for dump_stage in result["stages"]}) != len(
        result["stages"]
    ):
        raise StateError("多个阶段不能匹配同一个值")
    definitions = {d["id"]: d for d in existing}
    definitions[state_id] = result
    for field in ["interactions", "events"]:
        result[field] = [
            rule(r, definitions, state_id, event=field == "events")
            for r in raw.get(field, [])
        ]
        ids = [r["id"] for r in result[field]]
        if len(ids) != len(set(ids)):
            raise StateError("同一状态的交互或事件标识不能重复")
    return result


def stage_for(definition, value):
    for stage in definition["stages"]:
        if definition["type"] == "number":
            # Adjacent stages use [min, max); the state's maximum belongs to its final stage.
            low, high = stage["min"], stage["max"]
            if (low is None or value >= low) and (
                high is None or value < high or value == high == definition["max"]
            ):
                return stage
        elif stage["value"] == value:
            return stage
    return {"name": "", "description": ""}


def references(definition):
    for field in ["interactions", "events"]:
        for item in definition[field]:
            for part in item["conditions"] + item["effects"]:
                yield part["state"]
