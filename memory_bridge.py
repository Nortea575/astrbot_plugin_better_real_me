"""Narrow, optional bridge from life events to LivingMemory 2.9.1."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

LIVINGMEMORY_PLUGIN_ID = "astrbot_plugin_livingmemory"
LIFE_MEMORY_SOURCE = "虚拟角色生活"
LIFE_SCOPE_PREFIX = "livingmemory:life:"
DEFAULT_PERSONA_ID = "default"


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _iso_timestamp(value: Any) -> tuple[str, float]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("生活事件缺少有效发生时间")
    text = value.strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))  # noqa: FURB162
    if parsed.tzinfo is None:
        raise ValueError("生活事件时间必须包含时区")
    parsed = parsed.astimezone(UTC)
    return parsed.isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    ), parsed.timestamp()


class LifeMemoryBridge:
    """Sync pending life events through LivingMemory's public in-process API."""

    def __init__(self, context: Any, life: Any, engine: Any) -> None:
        self.context = context
        self.life = life
        self.engine = engine
        self._sync_lock = getattr(engine, "_brm_life_memory_sync_lock", None)
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()
            engine._brm_life_memory_sync_lock = self._sync_lock
        self._ensure_receipts()

    def _ensure_receipts(self) -> None:
        with self.engine.transaction():
            self.engine.db.execute(
                "CREATE TABLE IF NOT EXISTS brm_life_memory_receipts ("
                "event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL DEFAULT '', "
                "content_hash TEXT NOT NULL DEFAULT '', saved_at REAL NOT NULL)"
            )

    def _get_livingmemory_engine(self) -> Any | None:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter(LIVINGMEMORY_PLUGIN_ID)
        except Exception:  # noqa: BLE001
            return None
        if _value(metadata, "activated", default=True) is False:
            return None
        plugin = _value(metadata, "star_cls")
        if _value(plugin, "_terminating", default=False) is True:
            return None
        initializer = _value(plugin, "initializer")
        if initializer is None or _value(initializer, "is_initialized") is not True:
            return None
        return _value(initializer, "memory_engine")

    def status(self) -> dict[str, Any]:
        """Read registration state without initializing or querying memory services."""
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return {"installed": False, "ready": False, "version": ""}
        try:
            metadata = getter(LIVINGMEMORY_PLUGIN_ID)
        except Exception:  # noqa: BLE001
            metadata = None
        if metadata is None:
            return {"installed": False, "ready": False, "version": ""}
        plugin = _value(metadata, "star_cls")
        initializer = _value(plugin, "initializer")
        version = _value(metadata, "version", "plugin_version")
        if not version:
            version = _value(plugin, "version", "plugin_version")
        if not version:
            version = _value(initializer, "version", "plugin_version")
        activated = _value(metadata, "activated", default=True) is not False
        terminating = _value(plugin, "_terminating", default=False) is True
        ready = (
            activated
            and not terminating
            and initializer is not None
            and _value(initializer, "is_initialized") is True
            and _value(initializer, "memory_engine") is not None
        )
        return {
            "installed": True,
            "ready": bool(ready),
            "version": str(version or ""),
        }

    def _adapter_id(self, platform_id: str) -> str | None:
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        try:
            adapters = (
                get_insts()
                if callable(get_insts)
                else getattr(manager, "platform_insts", ())
            )
        except Exception:  # noqa: BLE001
            return None
        for adapter in adapters or ():
            meta_method = getattr(adapter, "meta", None)
            try:
                meta = meta_method() if callable(meta_method) else None
            except Exception:  # noqa: BLE001, S112
                continue
            adapter_id = _value(meta, "id")
            if isinstance(adapter_id, str) and adapter_id == platform_id:
                return adapter_id
        return None

    @staticmethod
    def scope_for_account(account_id: str) -> str:
        platform, separator, qq = str(account_id or "").partition(":")
        if (
            not separator
            or not platform
            or not qq.isascii()
            or not qq.isdigit()
            or not qq.strip("0")
        ):
            raise ValueError("生活记忆需要有效的平台账号身份")
        return f"{LIFE_SCOPE_PREFIX}{platform}:{qq}"

    @staticmethod
    def event_content(event: dict[str, Any]) -> str:
        name = str(event.get("name") or "生活中的一件事").strip()
        description = str(event.get("description") or "").strip()
        kind = str(event.get("kind") or "life_event").strip()
        when = str(event.get("occurred_at") or "").strip()
        details = event.get("details")
        parts = [f"角色在生活中完成了：{name}。"]
        if description:
            parts.append(description)
        if isinstance(details, dict):
            summary = details.get("summary")
            if isinstance(summary, str) and summary.strip():
                parts.append(summary.strip())
        parts.append(f"事件类型：{kind}；发生时间：{when}。")
        event_id = str(event.get("event_id") or event.get("id") or "")
        if event_id:
            marker = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
            parts.append(f"[BRM事件:life-{marker}]")
        return " ".join(parts)

    @staticmethod
    def _memory_id(result: Any) -> str:
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, int) and not isinstance(result, bool):
            return str(result)
        value = _value(result, "doc_id", "memory_id", "id", "uuid", default="")
        return str(value or "").strip()

    @staticmethod
    def _items(result: Any) -> list[Any]:
        if result is None:
            return []
        if isinstance(result, list):
            return result
        if isinstance(result, tuple):
            return list(result)
        for key in ("results", "memories", "items", "records"):
            value = _value(result, key)
            if isinstance(value, (list, tuple)):
                return list(value)
        if isinstance(result, dict):
            return [result]
        if _value(result, "content") is not None:
            return [result]
        return []

    @staticmethod
    def _page_match(result: Any, event_id: str, marker: str) -> tuple[bool, str]:
        for item in LifeMemoryBridge._items(result):
            raw_metadata = _value(item, "metadata", "meta", default={}) or {}
            if isinstance(raw_metadata, str):
                with suppress(ValueError, TypeError):
                    raw_metadata = json.loads(raw_metadata)
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            candidate_id = str(
                _value(item, "doc_id", "memory_id", "id", "uuid", default="") or ""
            )
            candidate_event = metadata.get("event_id") or metadata.get("brm_event_id")
            candidate_content = _value(item, "text", "content", "memory", default="")
            if candidate_event == event_id or (
                isinstance(candidate_content, str) and marker in candidate_content
            ):
                return True, candidate_id
        return False, ""

    def _receipt(self, event_id: str) -> str | None:
        with self.engine.lock:
            row = self.engine.db.execute(
                "SELECT memory_id FROM brm_life_memory_receipts WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return str(row["memory_id"] or "") if row is not None else None

    def _save_receipt(self, event_id: str, memory_id: str, content: str) -> None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO brm_life_memory_receipts(event_id,memory_id,content_hash,saved_at) "
                "VALUES (?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                "memory_id=CASE WHEN excluded.memory_id<>'' THEN excluded.memory_id ELSE brm_life_memory_receipts.memory_id END, "
                "content_hash=excluded.content_hash, saved_at=excluded.saved_at",
                (event_id, memory_id, digest, float(self.engine.clock())),
            )

    async def _find_existing(
        self,
        memory_engine: Any,
        *,
        marker: str,
        scope: str,
        event_id: str,
    ) -> tuple[bool, str]:
        list_page = getattr(memory_engine, "list_memories_page", None)
        if not callable(list_page):
            raise RuntimeError("LivingMemory 未提供 list_memories_page")  # noqa: TRY004
        result = await _maybe_await(
            list_page(
                session_id=scope,
                keyword=marker,
                status="all",
                page=1,
                page_size=20,
            )
        )
        return self._page_match(result, event_id, marker)

    async def sync_event(self, event: dict[str, Any], persona_id: str = "") -> bool:
        """Store one pending event once; false means it should remain pending."""
        event_id = str(event.get("event_id") or event.get("id") or "").strip()
        if not event_id:
            raise ValueError("生活事件缺少稳定标识")

        async with self._sync_lock:
            receipt = self._receipt(event_id)
            if receipt is not None:
                self.life.mark_memory(event_id, "saved", memory_id=receipt)
                return True

            memory_engine = self._get_livingmemory_engine()
            if memory_engine is None:
                return False

            account_id = str(event.get("account_id") or "")
            platform_id = account_id.partition(":")[0]
            adapter_id = self._adapter_id(platform_id)
            if adapter_id is None:
                return False

            scope = self.scope_for_account(account_id)
            occurred_at, create_time = _iso_timestamp(event.get("occurred_at"))

            content = self.event_content({**event, "occurred_at": occurred_at})
            marker_match = re.search(r"\[BRM事件:(life-[a-f0-9]{24})\]", content)
            marker = marker_match.group(0) if marker_match else ""
            if not marker:
                raise ValueError("无法生成生活事件幂等标识")
            try:
                found, existing_id = await self._find_existing(
                    memory_engine,
                    marker=marker,
                    scope=scope,
                    event_id=event_id,
                )
            except Exception:  # noqa: BLE001
                # Do not write when the preflight lookup failed; this closes the retry gap
                # left by LivingMemory 2.9.1's lack of a native idempotency key.
                return False
            if found:
                self._save_receipt(event_id, existing_id, content)
                self.life.mark_memory(event_id, "saved", memory_id=existing_id)
                return True

            add_memory = getattr(memory_engine, "add_memory", None)
            if not callable(add_memory):
                return False
            metadata = {
                "adapter_id": adapter_id,
                "source": LIFE_MEMORY_SOURCE,
                "source_plugin": "astrbot_plugin_better_real_me",
                "actor_key": account_id,
                "event_id": event_id,
                "occurred_at": occurred_at,
                "kind": str(event.get("kind") or "life_event"),
                "create_time": create_time,
            }
            result = await _maybe_await(
                add_memory(
                    content=content,
                    session_id=scope,
                    persona_id=persona_id or DEFAULT_PERSONA_ID,
                    importance=0.6,
                    metadata=metadata,
                    preserve_create_time=True,
                )
            )
            memory_id = self._memory_id(result)
            self._save_receipt(event_id, memory_id, content)
            self.life.mark_memory(event_id, "saved", memory_id=memory_id)
            return True

    async def relevant_memories(
        self,
        account_id: str,
        persona_id: str,
        query: str,
        *,
        limit: int = 3,
    ) -> list[str]:
        memory_engine = self._get_livingmemory_engine()
        if memory_engine is None:
            return []
        try:
            scope = self.scope_for_account(account_id)
            search = getattr(memory_engine, "search_memories", None)
            if not callable(search):
                return []
            result = await _maybe_await(
                search(
                    query=query[:1200],
                    k=max(1, min(int(limit), 5)),
                    session_id=scope,
                    persona_id=persona_id or DEFAULT_PERSONA_ID,
                )
            )
        except Exception:  # noqa: BLE001
            return []
        snippets = []
        for item in self._items(result)[: max(1, min(int(limit), 5))]:
            content = _value(item, "content", "text", "memory", default="")
            if isinstance(content, str) and content.strip():
                clean = re.sub(r"\s*\[BRM事件:life-[a-f0-9]{24}\]", "", content).strip()
                if clean:
                    snippets.append(clean[:500])
        return snippets
