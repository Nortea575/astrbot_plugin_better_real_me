"""Validation and time calculations for recurring local schedules."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from datetime import time as datetime_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import StateError, identifier, number, text

_DAY_MINUTES = 24 * 60
_WEEK_MINUTES = 7 * _DAY_MINUTES


def default_schedule():
    return {"timezone": "Asia/Shanghai", "categories": [], "entries": []}


def _minutes(value, label):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ):
        raise StateError(f"{label}必须使用 HH:mm 格式")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise StateError(f"无效时区：{name}") from exc


def _entry_intervals(entry):
    start = _minutes(entry["start"], "开始时间")
    end = _minutes(entry["end"], "结束时间")
    duration = (end - start) % _DAY_MINUTES or _DAY_MINUTES
    intervals = []
    for weekday in entry["days"]:
        begin = weekday * _DAY_MINUTES + start
        finish = begin + duration
        if finish <= _WEEK_MINUTES:
            intervals.append((begin, finish))
        else:
            intervals.extend(((begin, _WEEK_MINUTES), (0, finish - _WEEK_MINUTES)))
    return intervals


def _validate_overlaps(entries):
    enabled = [entry for entry in entries if entry["enabled"]]
    intervals = {entry["id"]: _entry_intervals(entry) for entry in enabled}
    for index, first in enumerate(enabled):
        for second in enabled[index + 1 :]:
            if any(
                max(a, c) < min(b, d)
                for a, b in intervals[first["id"]]
                for c, d in intervals[second["id"]]
            ):
                raise StateError(
                    f"日程“{first['name']}”({first['id']})与“{second['name']}”({second['id']})时间重叠"
                )


def apply_category_changes(actions, changes):
    """Return actions with explicitly renamed or deleted schedule categories."""
    if changes is None:
        return [dict(action) for action in actions]
    if not isinstance(changes, dict):
        raise StateError("日程分类变更必须为对象")
    raw_renames = changes.get("rename", {})
    raw_deletes = changes.get("delete", [])
    if not isinstance(raw_renames, dict) or not isinstance(raw_deletes, list):
        raise StateError("日程分类改名或删除配置格式错误")

    renames = {}
    for raw_old, raw_new in raw_renames.items():
        old = text(raw_old, "待改名的日程分类", True)
        new = text(raw_new, "新日程分类名称", True)
        renames[old] = new
    deletes = []
    for raw_name in raw_deletes:
        name = text(raw_name, "待删除的日程分类", True)
        if name in deletes:
            raise StateError("待删除的日程分类不能重复")
        deletes.append(name)
    if set(renames) & set(deletes):
        raise StateError("同一日程分类不能同时改名和删除")
    if set(renames.values()) & set(deletes):
        raise StateError("改名后的日程分类不能同时删除")

    updated = []
    for action in actions:
        item = dict(action)
        category = item.get("category", "")
        if category in renames:
            item["category"] = renames[category]
        elif category in deletes:
            item["category"] = ""
        updated.append(item)
    return updated


def normalize_schedule(raw, definitions):
    """Validate a schedule and return its normalized, persistence-ready form."""
    if not isinstance(raw, dict):
        raise StateError("日程配置必须为对象")
    timezone_name = text(raw.get("timezone", "Asia/Shanghai"), "时区", True)
    _zone(timezone_name)
    raw_categories = raw.get("categories", [])
    if not isinstance(raw_categories, list):
        raise StateError("日程分类必须为列表")
    categories = []
    for raw_category in raw_categories:
        category = text(raw_category, "日程分类", True)
        if category in categories:
            raise StateError("日程分类名称不能重复")
        categories.append(category)
    raw_entries = raw.get("entries", [])
    if not isinstance(raw_entries, list):
        raise StateError("日程条目必须为列表")

    state_by_id = {state["id"]: state for state in definitions}
    entries = []
    seen_ids = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise StateError("日程条目格式错误")
        entry_id = identifier(raw_entry.get("id"), "日程标识")
        if entry_id in seen_ids:
            raise StateError("日程标识不能重复")
        seen_ids.add(entry_id)

        days = raw_entry.get("days")
        if not isinstance(days, list) or not days:
            raise StateError("日程至少需要选择一天")
        if any(
            isinstance(day, bool) or not isinstance(day, int) or day < 0 or day > 6
            for day in days
        ):
            raise StateError("日程星期应为 0 至 6（周一至周日）")
        if len(days) != len(set(days)):
            raise StateError("同一日程不能重复选择星期")
        start = raw_entry.get("start")
        end = raw_entry.get("end")
        _minutes(start, "开始时间")
        _minutes(end, "结束时间")

        raw_rates = raw_entry.get("rates", {})
        if not isinstance(raw_rates, dict):
            raise StateError("日程变化量必须为对象")
        rates = {}
        for state_id, rate in raw_rates.items():
            state = state_by_id.get(state_id)
            if state is None:
                raise StateError(f"日程引用了不存在的状态：{state_id}")
            if state["type"] != "number":
                raise StateError(f"日程变化量只能引用数值状态：{state['name']}")
            rates[state_id] = number(rate, f"{state['name']}每小时变化量")

        category = text(raw_entry.get("category", ""), "日程分类")
        if category and category not in categories:
            if "categories" in raw:
                raise StateError(f"日程条目引用了不存在的分类：{category}")
            categories.append(category)

        entries.append(
            {
                "id": entry_id,
                "name": text(raw_entry.get("name", ""), "日程名称", True),
                "description": text(raw_entry.get("description", ""), "日程说明"),
                "category": category,
                "enabled": bool(raw_entry.get("enabled", True)),
                "days": sorted(days),
                "start": start,
                "end": end,
                "rates": rates,
            }
        )

    _validate_overlaps(entries)
    return {"timezone": timezone_name, "categories": categories, "entries": entries}


def _occurrence(entry, start_day, zone):
    start_minute = _minutes(entry["start"], "开始时间")
    end_minute = _minutes(entry["end"], "结束时间")
    start_dt = datetime.combine(
        start_day, datetime_time(start_minute // 60, start_minute % 60), zone
    )
    if end_minute <= start_minute:
        end_day = start_day + timedelta(days=1)
    else:
        end_day = start_day
    end_dt = datetime.combine(
        end_day, datetime_time(end_minute // 60, end_minute % 60), zone
    )
    return start_dt.timestamp(), end_dt.timestamp()


def _occurrences_near(schedule, now_ts):
    zone = _zone(schedule["timezone"])
    local_date = datetime.fromtimestamp(now_ts, zone).date()
    occurrences = []
    for offset in range(-1, 9):
        start_day = local_date + timedelta(days=offset)
        for entry in schedule["entries"]:
            if entry["enabled"] and start_day.weekday() in entry["days"]:
                start_ts, end_ts = _occurrence(entry, start_day, zone)
                occurrences.append((start_ts, end_ts, entry))
    return zone, sorted(occurrences, key=lambda item: item[0])


def _runtime_entry(entry, start_ts, end_ts, zone):
    return {
        "id": entry["id"],
        "name": entry["name"],
        "description": entry["description"],
        "category": entry["category"],
        "start_at": datetime.fromtimestamp(start_ts, zone).isoformat(
            timespec="seconds"
        ),
        "end_at": datetime.fromtimestamp(end_ts, zone).isoformat(timespec="seconds"),
        "rates": dict(entry["rates"]),
    }


def schedule_runtime(schedule, now_ts):
    """Return the current local time and the active and next schedule windows."""
    zone, occurrences = _occurrences_near(schedule, now_ts)
    active = next((item for item in occurrences if item[0] <= now_ts < item[1]), None)
    upcoming = next((item for item in occurrences if item[0] > now_ts), None)
    return {
        "local_time": datetime.fromtimestamp(now_ts, zone).isoformat(
            timespec="seconds"
        ),
        "active": _runtime_entry(active[2], active[0], active[1], zone)
        if active
        else None,
        "next": _runtime_entry(upcoming[2], upcoming[0], upcoming[1], zone)
        if upcoming
        else None,
    }


def active_entry(schedule, timestamp):
    """Find the entry active at a Unix timestamp, if any."""
    _, occurrences = _occurrences_near(schedule, timestamp)
    match = next((item for item in occurrences if item[0] <= timestamp < item[1]), None)
    return match[2] if match else None


def boundaries(schedule, start_ts, end_ts):
    """List schedule transitions strictly inside an elapsed-time interval."""
    if end_ts <= start_ts or not schedule["entries"]:
        return []
    zone = _zone(schedule["timezone"])
    first_day = datetime.fromtimestamp(start_ts, zone).date() - timedelta(days=1)
    last_day = datetime.fromtimestamp(end_ts, zone).date() + timedelta(days=1)
    points = set()
    day = first_day
    while day <= last_day:
        for entry in schedule["entries"]:
            if entry["enabled"] and day.weekday() in entry["days"]:
                start, end = _occurrence(entry, day, zone)
                if start_ts < start < end_ts:
                    points.add(start)
                if start_ts < end < end_ts:
                    points.add(end)
        day += timedelta(days=1)
    return sorted(points)
