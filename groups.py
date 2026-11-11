"""Normalization and evaluation for read-only composite state groups."""

from __future__ import annotations

import itertools
import math

from .schema import StateError, identifier, number, text


def normalize_group(raw, definitions):
    """Validate a composite group against the available base state definitions.

    Args:
        raw: User supplied group definition.
        definitions: Base state definitions indexed by their identifier.

    Returns:
        A normalized group definition with numeric state metadata.

    Raises:
        StateError: If the group shape, scope, factors, or references are invalid.
    """
    if not isinstance(raw, dict):
        raise StateError("综合指标定义必须为对象")

    group_id = identifier(raw.get("id"), "综合指标标识")
    scope = raw.get("scope", "character")
    if scope not in {"character", "conversation", "user"}:
        raise StateError("不支持的综合指标归属")

    factors = raw.get("factors")
    if not isinstance(factors, list) or len(factors) < 2:
        raise StateError("综合指标至少需要两个基础数值状态来源")
    normalized_factors = []
    source_ids = set()
    allowed_scopes = {
        "character": {"character"},
        "conversation": {"character", "conversation"},
        "user": {"character", "user"},
    }[scope]
    total_weight = 0.0

    for factor in factors:
        if not isinstance(factor, dict):
            raise StateError("综合指标来源格式错误")
        state_id = identifier(factor.get("state_id"), "来源状态标识")
        if state_id in source_ids:
            raise StateError("综合指标不能重复引用同一基础状态")
        source_ids.add(state_id)
        source = definitions.get(state_id)
        if source is None:
            raise StateError(f"综合指标引用了不存在的基础状态：{state_id}")
        if source.get("type") != "number":
            raise StateError(f"来源状态“{source['name']}”必须是数值状态")
        if source.get("scope") not in allowed_scopes:
            raise StateError("来源状态归属与综合指标归属不兼容")

        weight = number(factor.get("weight"), "来源权重")
        if weight < 0:
            raise StateError("来源权重不能小于零")
        low = number(factor.get("low"), "评分下界")
        high = number(factor.get("high"), "评分上界")
        if high <= low or not math.isfinite(high - low):
            raise StateError("评分上界必须大于评分下界")
        direction = factor.get("direction", "positive")
        if direction not in {"positive", "negative"}:
            raise StateError("来源方向应为 positive 或 negative")
        total_weight += weight
        normalized_factors.append(
            {
                "state_id": state_id,
                "weight": weight,
                "low": low,
                "high": high,
                "direction": direction,
            }
        )

    if not math.isfinite(total_weight) or total_weight <= 0:
        raise StateError("来源权重总和必须大于零")

    stages = []
    for stage in raw.get("stages", []):
        if not isinstance(stage, dict):
            raise StateError("综合指标阶段格式错误")
        entry = {
            "name": text(stage.get("name", ""), "阶段名称", True),
            "description": text(stage.get("description", ""), "阶段描述"),
            "min": stage.get("min"),
            "max": stage.get("max"),
        }
        for key in ("min", "max"):
            if entry[key] is not None:
                entry[key] = number(entry[key], "阶段边界")
        if (
            entry["min"] is not None
            and entry["max"] is not None
            and entry["min"] >= entry["max"]
        ):
            raise StateError("阶段下界应小于上界")
        stages.append(entry)
    ordered_stages = sorted(
        stages,
        key=lambda stage: float("-inf") if stage["min"] is None else stage["min"],
    )
    for previous, following in itertools.pairwise(ordered_stages):
        if (
            previous["max"] is None
            or following["min"] is None
            or previous["max"] > following["min"]
        ):
            raise StateError("阶段区间不能重叠")

    return {
        "id": group_id,
        "name": text(raw.get("name", ""), "综合指标名称", True),
        "description": text(raw.get("description", ""), "指标含义"),
        "category": text(raw.get("category", ""), "分类"),
        "scope": scope,
        "enabled": bool(raw.get("enabled", True)),
        "inject": bool(raw.get("inject", True)),
        "show_value": bool(raw.get("show_value", True)),
        "prompt": text(
            raw.get("prompt", "{name}：{value}；{stage}。{description}"), "描述模板"
        ),
        "stages": stages,
        "factors": normalized_factors,
        "kind": "group",
        "type": "number",
        "min": 0,
        "max": 100,
    }


def evaluate_group(group, definitions, values):
    """Calculate a weighted 0–100 value and its per-source contribution rows.

    Args:
        group: Normalized composite group definition.
        definitions: Base state definitions indexed by identifier.
        values: Current values indexed by base state identifier.

    Returns:
        A tuple containing the weighted value and component detail rows.
    """
    weight_total = sum(factor["weight"] for factor in group["factors"])
    components = []
    value = 0.0
    for factor in group["factors"]:
        state_id = factor["state_id"]
        source_value = values[state_id]
        if isinstance(source_value, bool) or not isinstance(source_value, (int, float)):
            raise StateError(f"来源状态“{definitions[state_id]['name']}”必须为数值")
        score = (source_value - factor["low"]) / (factor["high"] - factor["low"]) * 100
        score = min(100.0, max(0.0, score))
        if factor["direction"] == "negative":
            score = 100.0 - score
        weight_ratio = factor["weight"] / weight_total
        contribution = score * weight_ratio
        value += contribution
        components.append(
            {
                "state_id": state_id,
                "name": definitions[state_id]["name"],
                "value": source_value,
                "score": score,
                "weight": factor["weight"],
                "weight_percent": weight_ratio * 100,
                "contribution": contribution,
                "low": factor["low"],
                "high": factor["high"],
                "direction": factor["direction"],
            }
        )
    return min(100.0, max(0.0, value)), components
