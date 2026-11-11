"""Hierarchical indoor groups and room graph validation/path finding."""

from __future__ import annotations

from collections import deque

from .schema import StateError, identifier, text

ROOT_GROUP_ID = "district"
HOME_GROUP_ID = "home"
ROOT_GROUP_NAME = "街区"
HOME_GROUP_NAME = "家"


def _available_id(preferred, used):
    candidate = preferred
    suffix = 2
    while candidate in used:
        candidate = f"{preferred}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def default_indoor():
    """Return an empty but navigable environment with one root group."""
    return {
        "schema_version": 2,
        "root_group_id": ROOT_GROUP_ID,
        "groups": [
            {
                "id": ROOT_GROUP_ID,
                "name": ROOT_GROUP_NAME,
                "description": "",
                "parent_id": "",
                "entry_room_id": "",
            }
        ],
        "rooms": [],
        "connections": [],
        "initial_room_id": "",
    }


def _legacy_groups(raw_rooms, initial_room_id):
    used = {
        identifier(item.get("id"), "房间标识")
        for item in raw_rooms
        if isinstance(item, dict)
    }
    root_id = _available_id(ROOT_GROUP_ID, used)
    home_id = _available_id(HOME_GROUP_ID, used)
    entry_id = ""
    aliases = {
        "玄关",
        "门厅",
        "入户",
        "入户门",
        "门口",
        "前厅",
        "迎宾厅",
        "foyer",
        "entry",
        "entrance",
        "vestibule",
    }
    for raw_room in raw_rooms:
        if (
            isinstance(raw_room, dict)
            and str(raw_room.get("name", "")).strip().casefold() in aliases
        ):
            entry_id = raw_room.get("id", "")
            break
    if not entry_id:
        entry_id = initial_room_id or ""
    return root_id, home_id, entry_id


def normalize_indoor(raw):
    """Validate and normalize a hierarchical room graph configuration.

    Legacy flat room graphs are migrated to a root ``街区`` group containing a
    ``家`` child. Their room IDs, connections, and initial room remain intact.
    Connection endpoints may be group IDs from the editor; these are resolved
    to the group's configured entry room before persistence.
    """
    if not isinstance(raw, dict):
        raise StateError("环境配置必须为对象")

    raw_rooms = raw.get("rooms")
    raw_connections = raw.get("connections")
    if not isinstance(raw_rooms, list):
        raise StateError("房间列表格式错误")
    if not isinstance(raw_connections, list):
        raise StateError("房间连接列表格式错误")

    raw_groups = raw.get("groups")
    legacy = raw_groups is None
    if legacy:
        initial = raw.get("initial_room_id", "")
        if initial:
            initial = identifier(initial, "初始房间标识")
        if raw_rooms:
            root_id, home_id, home_entry = _legacy_groups(raw_rooms, initial)
            groups_source = [
                {
                    "id": root_id,
                    "name": ROOT_GROUP_NAME,
                    "description": "",
                    "parent_id": "",
                    "entry_room_id": "",
                },
                {
                    "id": home_id,
                    "name": HOME_GROUP_NAME,
                    "description": "",
                    "parent_id": root_id,
                    "entry_room_id": home_entry,
                },
            ]
            rooms_source = [
                {**room, "group_id": home_id} if isinstance(room, dict) else room
                for room in raw_rooms
            ]
            root_group_id = root_id
        else:
            groups_source = default_indoor()["groups"]
            rooms_source = []
            root_group_id = ROOT_GROUP_ID
    else:
        if raw.get("schema_version", 2) != 2:
            raise StateError("不支持的环境配置版本")
        if not isinstance(raw_groups, list):
            raise StateError("分组列表格式错误")
        groups_source = raw_groups
        rooms_source = raw_rooms
        root_group_id = raw.get("root_group_id", "")

    groups = []
    group_ids = set()
    for raw_group in groups_source:
        if not isinstance(raw_group, dict):
            raise StateError("分组格式错误")
        group_id = identifier(raw_group.get("id"), "分组标识")
        if group_id in group_ids:
            raise StateError(f"分组标识重复：{group_id}")
        group_ids.add(group_id)
        parent = raw_group.get("parent_id", "")
        if parent is None:
            parent = ""
        elif parent:
            parent = identifier(parent, "父分组标识")
        entry_room_id = raw_group.get("entry_room_id", "")
        if entry_room_id:
            entry_room_id = identifier(entry_room_id, "分组入口房间标识")
        groups.append(
            {
                "id": group_id,
                "name": text(raw_group.get("name"), "分组名称", required=True),
                "description": text(raw_group.get("description", ""), "分组描述"),
                "parent_id": parent,
                "entry_room_id": entry_room_id,
            }
        )
    if not groups:
        raise StateError("至少需要一个根分组")

    rooms = []
    room_ids = set()
    for raw_room in rooms_source:
        if not isinstance(raw_room, dict):
            raise StateError("房间格式错误")
        room_id = identifier(raw_room.get("id"), "房间标识")
        if room_id in room_ids:
            raise StateError(f"房间标识重复：{room_id}")
        room_ids.add(room_id)
        group_id = identifier(raw_room.get("group_id"), "房间所属分组标识")
        rooms.append(
            {
                "id": room_id,
                "name": text(raw_room.get("name"), "房间名称", required=True),
                "description": text(raw_room.get("description", ""), "房间描述"),
                "group_id": group_id,
            }
        )

    overlap = group_ids & room_ids
    if overlap:
        raise StateError("分组和房间标识不能重复：" + ", ".join(sorted(overlap)))

    groups_by_id = {group["id"]: group for group in groups}
    if not root_group_id:
        roots = [group["id"] for group in groups if not group["parent_id"]]
        if len(roots) == 1:
            root_group_id = roots[0]
    root_group_id = identifier(root_group_id, "根分组标识")
    if root_group_id not in groups_by_id:
        raise StateError(f"根分组不存在：{root_group_id}")
    roots = [group["id"] for group in groups if not group["parent_id"]]
    if roots != [root_group_id] and set(roots) != {root_group_id}:
        raise StateError("必须且只能有一个与根分组标识一致的无父分组")

    for group in groups:
        parent = group["parent_id"]
        if group["id"] == root_group_id:
            if parent:
                raise StateError("根分组不能有父分组")
        elif not parent or parent not in groups_by_id:
            raise StateError(f"分组“{group['id']}”的父分组不存在")

    children = {group_id: [] for group_id in group_ids}
    for group in groups:
        if group["parent_id"]:
            children[group["parent_id"]].append(group["id"])
    reached = set()
    pending = [root_group_id]
    while pending:
        group_id = pending.pop()
        if group_id in reached:
            continue
        reached.add(group_id)
        pending.extend(children[group_id])
    if reached != group_ids:
        raise StateError("分组必须构成无循环的单根树")

    room_by_id = {room["id"]: room for room in rooms}
    for room in rooms:
        if room["group_id"] not in groups_by_id:
            raise StateError(f"房间“{room['id']}”所属分组不存在")
    for group in groups:
        entry_id = group["entry_room_id"]
        if entry_id:
            room = room_by_id.get(entry_id)
            if room is None:
                raise StateError(f"分组“{group['id']}”的入口房间不存在：{entry_id}")
            if not _is_group_ancestor(groups_by_id, group["id"], room["group_id"]):
                raise StateError(f"分组“{group['id']}”的入口必须位于该组内")

    connections = []
    edge_keys = set()
    for raw_edge in raw_connections:
        if not isinstance(raw_edge, dict):
            raise StateError("房间连接格式错误")
        left = identifier(raw_edge.get("a"), "连接端点标识")
        right = identifier(raw_edge.get("b"), "连接端点标识")
        if left in groups_by_id:
            left = groups_by_id[left]["entry_room_id"]
            if not left:
                raise StateError("连接端点分组没有入口房间")
        if right in groups_by_id:
            right = groups_by_id[right]["entry_room_id"]
            if not right:
                raise StateError("连接端点分组没有入口房间")
        if left == right:
            raise StateError(f"不能连接房间自身：{left}")
        if left not in room_ids or right not in room_ids:
            missing = left if left not in room_ids else right
            raise StateError(f"连接引用了不存在的房间：{missing}")
        key = tuple(sorted((left, right)))
        if key in edge_keys:
            raise StateError(f"房间连接重复：{key[0]} ↔ {key[1]}")
        edge_keys.add(key)
        connections.append({"a": key[0], "b": key[1]})

    initial_room_id = raw.get("initial_room_id", "")
    if initial_room_id:
        initial_room_id = identifier(initial_room_id, "初始房间标识")
        if initial_room_id not in room_ids:
            raise StateError(f"初始房间不存在：{initial_room_id}")
    elif rooms:
        raise StateError("存在房间时必须指定有效的初始房间")
    else:
        initial_room_id = ""

    groups.sort(key=lambda group: group["id"])
    rooms.sort(key=lambda room: room["id"])
    connections.sort(key=lambda edge: (edge["a"], edge["b"]))
    return {
        "schema_version": 2,
        "root_group_id": root_group_id,
        "groups": groups,
        "rooms": rooms,
        "connections": connections,
        "initial_room_id": initial_room_id,
    }


def _is_group_ancestor(groups_by_id, ancestor_id, group_id):
    while group_id:
        if group_id == ancestor_id:
            return True
        group = groups_by_id.get(group_id)
        if group is None:
            return False
        group_id = group["parent_id"]
    return False


def group_path(indoor, group_id):
    """Return a root-to-group list of ``{id, name}`` records."""
    groups = {group["id"]: group for group in indoor["groups"]}
    chain = []
    current = groups.get(group_id)
    while current:
        chain.append({"id": current["id"], "name": current["name"]})
        current = groups.get(current["parent_id"])
    return list(reversed(chain))


def room_path(indoor, room_id):
    """Return a human-readable group and room path, or an empty string."""
    room = next((item for item in indoor["rooms"] if item["id"] == room_id), None)
    if not room:
        return ""
    names = [item["name"] for item in group_path(indoor, room["group_id"])]
    names.append(room["name"])
    return " / ".join(names)


def group_entry_room(indoor, group_id):
    group = next((item for item in indoor["groups"] if item["id"] == group_id), None)
    return group["entry_room_id"] if group else ""


def resolve_destination(indoor, current_room_id, target_id):
    """Resolve a room/group destination; ``None`` means invalid or no entry."""
    room_by_id = {room["id"]: room for room in indoor["rooms"]}
    if target_id in room_by_id:
        return target_id
    group_by_id = {group["id"]: group for group in indoor["groups"]}
    target_group = group_by_id.get(target_id)
    if target_group is None:
        return None
    current = room_by_id.get(current_room_id)
    if current and _is_group_ancestor(group_by_id, target_id, current["group_id"]):
        return current_room_id
    return target_group["entry_room_id"] or None


def shortest_path(indoor, start_room_id, target_room_id):
    """Return a deterministic shortest path in the undirected room graph."""
    rooms = {room["id"] for room in indoor["rooms"]}
    if start_room_id not in rooms or target_room_id not in rooms:
        return None
    if start_room_id == target_room_id:
        return [start_room_id]

    adjacent = {room_id: set() for room_id in rooms}
    for edge in indoor["connections"]:
        adjacent[edge["a"]].add(edge["b"])
        adjacent[edge["b"]].add(edge["a"])

    queue = deque([start_room_id])
    previous = {start_room_id: None}
    while queue:
        room_id = queue.popleft()
        for neighbor in sorted(adjacent[room_id]):
            if neighbor in previous:
                continue
            previous[neighbor] = room_id
            if neighbor == target_room_id:
                path = [target_room_id]
                while previous[path[-1]] is not None:
                    path.append(previous[path[-1]])
                return list(reversed(path))
            queue.append(neighbor)
    return None
