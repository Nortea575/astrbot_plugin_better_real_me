"""Plain-text renderers for the read-only QQ command surface."""

from __future__ import annotations

import json
from datetime import date, datetime

QUERY_COMMANDS = {"状态", "日程", "地点"}


def query_usage(prefix: str = "") -> str:
    start = f"{prefix}" if prefix else ""
    return "\n".join(
        (
            "用法：",
            f"{start}查询 状态 — 查看当前可见状态",
            f"{start}查询 日程 — 查看固定日程与生活活动",
            f"{start}查询 地点 — 查看当前位置与相邻出口",
        )
    )


def exact_query(text: str, command: str) -> bool:
    normalized = " ".join(str(text or "").strip().split())
    return normalized == f"查询 {command}"


def help_text(prefix: str = "") -> str:
    start = f"{prefix}" if prefix else ""
    return "\n".join(
        (
            "更好的真实生活：",
            f"{start}查询 状态 — 查看当前可见状态",
            f"{start}查询 日程 — 查看固定日程与生活活动",
            f"{start}查询 地点 — 查看当前位置与相邻出口",
            f"{start}帮助 — 查看命令帮助",
            "原有状态、日程、环境和状态交互指令仍可使用。",
        )
    )


def _rows(value):
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _ids(value):
    result = set()
    for item in _rows(value):
        identifier = item.get("id") if isinstance(item, dict) else item
        if isinstance(identifier, str) and identifier:
            result.add(identifier)
    return result


def resolve_mode(overview, account_id, *, life_state_ids=(), life_group_ids=()):
    """Select only the configured preset for one actor's QQ account."""
    overview = overview if isinstance(overview, dict) else {}
    account = next(
        (
            item
            for item in _rows(overview.get("accounts"))
            if isinstance(item, dict)
            and str(item.get("id") or item.get("account_id") or "") == str(account_id)
        ),
        {},
    )
    account_enabled = bool(account.get("enabled"))
    preset_id = str(account.get("preset_id") or "")
    preset = next(
        (
            item
            for item in _rows(overview.get("presets"))
            if isinstance(item, dict) and item.get("id") == preset_id
        ),
        {},
    )
    enabled = account_enabled and bool(preset_id) and bool(preset)
    contents = (
        preset.get("contents") if isinstance(preset.get("contents"), dict) else {}
    )
    names_by_state = {}
    names_by_group = {}
    for candidate in _rows(overview.get("presets")):
        if not isinstance(candidate, dict):
            continue
        candidate_contents = candidate.get("contents")
        if not isinstance(candidate_contents, dict):
            continue
        candidate_name = str(candidate.get("name") or candidate.get("id") or "生活预设")
        names_by_state.update(
            {
                identifier: candidate_name
                for identifier in _ids(candidate_contents.get("states"))
            }
        )
        names_by_group.update(
            {
                identifier: candidate_name
                for identifier in _ids(candidate_contents.get("groups"))
            }
        )
    return {
        "enabled": enabled,
        "account_id": str(account.get("id") or account.get("account_id") or ""),
        "preset_id": preset_id,
        "preset_name": str(preset.get("name") or preset_id),
        "state_ids": contents.get("states", []),
        "group_ids": contents.get("groups", []),
        "preset_names_by_state": names_by_state,
        "preset_names_by_group": names_by_group,
        "life_state_ids": _ids(life_state_ids),
        "life_group_ids": _ids(life_group_ids),
        "activity": account.get("current_activity") if enabled else None,
    }


def _category_names(categories):
    result = {}
    for item in _rows(categories):
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = str(item.get("name") or item["id"])
    return result


def _display_value(value):
    if value is None:
        return "无"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _owner(scope, *, is_life):
    if is_life:
        return ""
    if scope == "user":
        return "（当前用户独立）"
    if scope == "conversation":
        return "（当前会话独立）"
    return ""


def render_states(snapshot, mode=None):
    """Render only the definitions and computed rows visible in one snapshot.

    ``mode`` may contain ``enabled``, ``preset_id``, ``preset_name``,
    ``state_ids``, ``group_ids``, ``life_state_ids`` and ``life_group_ids``.
    The snapshot remains the authority for actor/session visibility.
    """
    mode = mode if isinstance(mode, dict) else {}
    mode_enabled = bool(mode.get("enabled"))
    selected_preset = str(mode.get("preset_id") or "") if mode_enabled else ""
    preset_name = str(mode.get("preset_name") or selected_preset or "")
    preset_state_ids = _ids(mode.get("state_ids"))
    preset_group_ids = _ids(mode.get("group_ids"))
    life_state_ids = _ids(mode.get("life_state_ids"))
    life_group_ids = _ids(mode.get("life_group_ids"))

    definitions = [
        item for item in _rows(snapshot.get("definitions")) if isinstance(item, dict)
    ]
    groups = [item for item in _rows(snapshot.get("groups")) if isinstance(item, dict)]
    state_rows = {
        str(item.get("id")): item
        for item in _rows(snapshot.get("states"))
        if isinstance(item, dict) and item.get("id")
    }
    category_names = _category_names(snapshot.get("categories"))

    sections = {}

    def add(definition, is_group):
        identifier = str(definition.get("id") or "")
        row = state_rows.get(identifier)
        if not identifier or row is None or not definition.get("enabled", True):
            return
        is_life = identifier in life_state_ids or identifier in life_group_ids
        if is_life:
            names = mode.get(
                "preset_names_by_group" if is_group else "preset_names_by_state", {}
            )
            preset_label = names.get(identifier) if isinstance(names, dict) else None
            selected_ids = preset_group_ids if is_group else preset_state_ids
            if not preset_label and identifier in selected_ids:
                preset_label = preset_name
            section = ("preset", str(preset_label or "生活预设状态"))
        else:
            section = ("user", "用户状态与派生组")

        category_id = str(definition.get("category") or "")
        category = category_names.get(category_id, category_id or "未分类")
        bucket = sections.setdefault(section, {}).setdefault(category, [])
        value = (
            _display_value(row.get("value"))
            if definition.get("show_value", True)
            else "不展示数值"
        )
        stage = str(row.get("stage") or "").strip()
        details = f"（{stage}）" if stage else ""
        owner = _owner(str(definition.get("scope") or "character"), is_life=is_life)
        bucket.append(
            f"{definition.get('name') or identifier}：{value}{details}{owner}"
        )

    for definition in definitions:
        add(definition, False)

    visible_state_ids = {
        str(item.get("id"))
        for item in definitions
        if item.get("enabled", True) and item.get("id")
    }
    for group in groups:
        factor_ids = {
            str(item.get("state_id"))
            for item in _rows(group.get("factors"))
            if isinstance(item, dict) and item.get("state_id")
        }
        if factor_ids and not factor_ids.issubset(visible_state_ids):
            continue
        add(group, True)

    lines = []
    for (kind, name), categories in sections.items():
        title = f"生活预设：{name}" if kind == "preset" else name
        lines.append(f"【{title}】")
        _append_categories(lines, categories)
    if not lines:
        return "当前没有可查看的启用状态。"
    return "\n".join(lines)


def _append_categories(lines, categories):
    for category, entries in categories.items():
        lines.append(f"{category}：")
        lines.extend(f"  {entry}" for entry in entries)


def render_schedule(
    snapshot, *, mode_name="", activity=None, windows=None, mode_enabled=False
):
    """Render fixed schedules and account-scoped life activity/window data."""
    runtime = snapshot.get("schedule_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    lines = []
    if runtime.get("local_time"):
        lines.append(f"本地时间：{runtime['local_time']}")

    if activity:
        if mode_name:
            lines.append(f"生活模式：{mode_name}")
        step_index = activity.get("step_index")
        step_count = activity.get("step_count")
        step = activity.get("step_name") or ""
        progress = f"第{step_index}/{step_count}步" if step_index and step_count else ""
        location = _activity_location(snapshot, activity)
        end = activity.get("ends_at") or ""
        paused = "（暂停中）" if activity.get("paused") else ""
        details = "、".join(
            item for item in (progress, step, f"地点：{location}") if item
        )
        suffix = f"，本步骤预计结束于 {end}" if end else ""
        lines.append(
            f"当前生活活动：{activity.get('name') or '进行中'}{paused}（{details}）{suffix}"
        )
    elif mode_enabled:
        if mode_name:
            lines.append(f"生活模式：{mode_name}")
        lines.append("当前没有进行中的生活活动。")
    else:
        lines.append("当前没有启用的生活模式。")

    active = runtime.get("active")
    if active:
        category = str(active.get("category") or "未分类")
        description = f"；{active['description']}" if active.get("description") else ""
        lines.append(
            f"固定日程·{category}：当前为{active.get('name') or '未命名日程'}{description}，至 {active.get('end_at') or '时间未知'}。"
        )
    else:
        lines.append("固定日程：当前没有活动日程。")

    upcoming = runtime.get("next")
    if upcoming:
        category = str(upcoming.get("category") or "未分类")
        description = (
            f"；{upcoming['description']}" if upcoming.get("description") else ""
        )
        lines.append(
            f"固定日程·{category}：下一项为{upcoming.get('name') or '未命名日程'}{description}，{upcoming.get('start_at') or '时间未知'} 至 {upcoming.get('end_at') or '时间未知'}。"
        )
    else:
        lines.append("固定日程：没有已启用的后续日程。")

    if not mode_enabled:
        lines.append("今日生活行动窗口：未启用生活模式。")
    else:
        window_data = windows if isinstance(windows, dict) else {"windows": windows}
        candidates = [
            item for item in _rows(window_data.get("windows")) if isinstance(item, dict)
        ]
        day_label = str(window_data.get("date") or "今日")
        timezone = str(window_data.get("timezone") or "")
        heading = f"按当前状态可选生活窗口（{day_label}"
        if timezone:
            heading += f"，{timezone}"
        if activity:
            heading += "；当前活动期间不并行"
        heading += "）："
        if not candidates:
            lines.append(heading)
            lines.append("  当前状态下没有符合条件的可选行动窗口。")
        else:
            lines.append(heading)
            for item in candidates:
                category = str(item.get("category") or "未分类")
                start = _window_clock(item.get("start_at"), day_label)
                end = _window_clock(item.get("end_at"), day_label)
                window = f"{start}–{end}" if start or end else "当前窗口"
                availability = "（当前可执行）" if item.get("available_now") else ""
                lines.append(
                    f"  {category}：{item.get('name') or '生活行动'}，{window}{availability}"
                )
    return "\n".join(lines)


def _window_clock(value, target_date):
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
        expected = date.fromisoformat(target_date) if target_date else parsed.date()
    except (TypeError, ValueError):
        return value
    delta = (parsed.date() - expected).days
    prefix = ""
    if delta == -1:
        prefix = "昨日 "
    elif delta < -1 or delta > 1:
        prefix = parsed.strftime("%m-%d ")
    elif delta == 1:
        prefix = "次日 "
    return prefix + parsed.strftime("%H:%M")


def _activity_location(snapshot, activity):
    runtime = snapshot.get("indoor_runtime") if isinstance(snapshot, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    current_room = runtime.get("room")
    current_room = current_room if isinstance(current_room, dict) else {}
    current_room_id = str(runtime.get("room_id") or current_room.get("id") or "")
    current_path = str(runtime.get("location_path") or "").strip()
    if not current_path and current_room:
        current_path = _room_path(snapshot, current_room_id, current_room.get("name"))

    target_room_id = str(activity.get("room_id") or "")
    target_path = _room_path(snapshot, target_room_id, activity.get("room_name") or "")
    target_differs = bool(target_room_id) and (
        target_room_id != current_room_id
        if current_room_id
        else bool(target_path and current_path and target_path != current_path)
    )
    if target_differs:
        if current_path:
            return f"当前位置：{current_path}；正在前往步骤目标：{target_path}"
        return f"正在前往步骤目标：{target_path}"
    if current_path:
        return f"当前位置：{current_path}"
    if target_path != "未指定地点":
        return f"步骤目标：{target_path}"
    return "未指定地点"


def _room_path(snapshot, room_id, fallback_name=""):
    indoor = snapshot.get("indoor") if isinstance(snapshot, dict) else {}
    indoor = indoor if isinstance(indoor, dict) else {}
    room = next(
        (
            item
            for item in _rows(indoor.get("rooms"))
            if isinstance(item, dict) and str(item.get("id") or "") == str(room_id)
        ),
        {},
    )
    if room:
        groups = {
            str(item.get("id")): item
            for item in _rows(indoor.get("groups"))
            if isinstance(item, dict) and item.get("id")
        }
        path = []
        group_id = str(room.get("group_id") or "")
        while group_id and group_id in groups:
            group = groups[group_id]
            path.append(str(group.get("name") or ""))
            group_id = str(group.get("parent_id") or "")
        path.reverse()
        path.append(str(room.get("name") or fallback_name or ""))
        return " / ".join(item for item in path if item) or "未指定地点"
    return str(fallback_name or "未指定地点")


def render_location(snapshot):
    """Render the complete room/group path and directly connected destinations."""
    indoor = snapshot.get("indoor")
    indoor = indoor if isinstance(indoor, dict) else {}
    rooms = _rows(indoor.get("rooms"))
    if not rooms:
        return "当前尚未配置可读取的地点。"

    runtime = snapshot.get("indoor_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    room = runtime.get("room")
    if not isinstance(room, dict):
        return "地点图已配置，但当前角色尚未记录所在地点。"

    group_path = [
        str(item.get("name") or "")
        for item in _rows(runtime.get("group_path"))
        if isinstance(item, dict) and item.get("name")
    ]
    room_name = str(room.get("name") or "未知地点")
    location_path = str(runtime.get("location_path") or "").strip()
    if not location_path:
        location_path = " / ".join([*group_path, room_name])
    groups = {
        str(item.get("id")): item
        for item in _rows(indoor.get("groups"))
        if isinstance(item, dict) and item.get("id")
    }
    current_group = groups.get(
        str(runtime.get("group_id") or room.get("group_id") or "")
    )
    lines = [f"完整位置：{location_path}"]
    if group_path:
        lines.append("组路径：" + " / ".join(group_path))
    if current_group:
        lines.append(f"当前组：{current_group.get('name') or '未命名分组'}")
    exits = [item for item in _rows(runtime.get("exits")) if isinstance(item, dict)]
    if exits:
        lines.append("相邻真实出口：")
        for destination in exits:
            path = str(destination.get("location_path") or "").strip()
            if not path:
                path_groups = [
                    str(item.get("name") or "")
                    for item in _rows(destination.get("group_path"))
                    if isinstance(item, dict) and item.get("name")
                ]
                path = " / ".join(
                    [*path_groups, str(destination.get("name") or "未知地点")]
                )
            lines.append(f"  {path}")
    else:
        lines.append("相邻真实出口：无。")
    return "\n".join(lines)
