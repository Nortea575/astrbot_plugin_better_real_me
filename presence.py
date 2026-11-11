"""Discover real QQ accounts connected through AstrBot's aiocqhttp adapter."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

_NO_RESPONSE = object()


class QQPresence:
    """Observe QQ adapter connections and report verified account identities."""

    poll_interval = 5.0
    request_timeout = 3.0
    connect_event_name = "meta_event.lifecycle.connect"

    def __init__(self, context: Any, on_account: Callable[..., Any]) -> None:
        self._context = context
        self._on_account = on_account
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_complete = asyncio.Event()
        self._handlers: dict[tuple[str, int], tuple[Any, Callable[..., Any]]] = {}
        self._confirmed_bots: set[tuple[str, int]] = set()
        self._reported_accounts: set[tuple[str, str]] = set()
        self._record_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start one background scan loop and return without waiting for I/O."""
        if self._closed or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """Stop discovery, detach this observer, and drain an active account write."""
        if self._closed:
            await self._close_complete.wait()
            return
        self._closed = True

        try:
            for bot, handler in self._handlers.values():
                with suppress(Exception):
                    bot.unsubscribe(self.connect_event_name, handler)
            self._handlers.clear()

            task = self._task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

            # Let a callback already in progress finish before its owner closes the database.
            async with self._record_lock:
                pass
        finally:
            self._close_complete.set()

    async def _run(self) -> None:
        while not self._closed:
            with suppress(Exception):
                await self._scan_once()
            await asyncio.sleep(self.poll_interval)

    async def _scan_once(self) -> None:
        probes = []
        for adapter in self._platforms():
            if self._closed:
                return
            details = self._adapter_details(adapter)
            if details is None:
                continue
            platform_id, bot = details
            key = (platform_id, id(bot))
            if key not in self._handlers and not self._attach_handler(
                platform_id, bot, key
            ):
                continue
            if key not in self._confirmed_bots:
                probes.append(self._query_connected_account(platform_id, bot, key))

        if probes:
            await asyncio.gather(*probes, return_exceptions=True)

    def _platforms(self) -> tuple[Any, ...]:
        manager = getattr(self._context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        with suppress(Exception):
            if callable(get_insts):
                return tuple(get_insts() or ())
            return tuple(getattr(manager, "platform_insts", ()) or ())
        return ()

    @staticmethod
    def _adapter_details(adapter: Any) -> tuple[str, Any] | None:
        metadata = None
        platform_id = None
        bot = None
        with suppress(Exception):
            metadata = adapter.meta()
            platform_id = getattr(metadata, "id", None)
            bot = getattr(adapter, "bot", None)

        if (
            getattr(metadata, "name", None) != "aiocqhttp"
            or not isinstance(platform_id, str)
            or not platform_id.strip()
            or bot is None
        ):
            return None
        return platform_id.strip(), bot

    def _attach_handler(self, platform_id: str, bot: Any, key: tuple[str, int]) -> bool:
        register = getattr(bot, "on_websocket_connection", None)
        unsubscribe = getattr(bot, "unsubscribe", None)
        if not callable(register) or not callable(unsubscribe):
            return False

        async def on_connection(event: Any) -> None:
            if self._closed:
                return
            qq = self._normalize_qq(self._event_value(event, "self_id"))
            if qq is None:
                return
            name = self._event_value(event, "nickname", "")
            await self._record_account(platform_id, qq, name, key)

        with suppress(Exception):
            register(on_connection)
            self._handlers[key] = (bot, on_connection)
            return True
        return False

    async def _query_connected_account(
        self, platform_id: str, bot: Any, key: tuple[str, int]
    ) -> None:
        if self._closed or key in self._confirmed_bots:
            return
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            return

        response = _NO_RESPONSE
        with suppress(Exception):
            response = await asyncio.wait_for(
                self._call_action(call_action), timeout=self.request_timeout
            )
        if response is _NO_RESPONSE:
            return

        profile = self._login_profile(response)
        if profile is None:
            return
        qq = self._normalize_qq(profile.get("user_id"))
        if qq is None:
            return
        await self._record_account(platform_id, qq, profile.get("nickname", ""), key)

    @staticmethod
    async def _call_action(call_action: Callable[..., Any]) -> Any:
        """Await CQHttp's coroutine on the current event loop."""
        return await call_action(action="get_login_info")

    @staticmethod
    def _login_profile(response: Any) -> Mapping[str, Any] | None:
        if not isinstance(response, Mapping):
            return None
        if response.get("status") == "failed":
            return None
        retcode = response.get("retcode")
        if retcode is not None and retcode != 0:
            return None
        profile = response.get("data", response)
        return profile if isinstance(profile, Mapping) else None

    async def _record_account(
        self,
        platform_id: str,
        qq_value: Any,
        name_value: Any,
        bot_key: tuple[str, int],
    ) -> None:
        qq = self._normalize_qq(qq_value)
        if qq is None or self._closed:
            return
        name = name_value.strip() if isinstance(name_value, str) else ""
        identity = (platform_id, qq)

        async with self._record_lock:
            if self._closed:
                return
            if identity not in self._reported_accounts:
                with suppress(Exception):
                    result = self._on_account(platform_id, qq, name)
                    if inspect.isawaitable(result):
                        await result
                    self._reported_accounts.add(identity)
                if identity not in self._reported_accounts:
                    return
            self._confirmed_bots.add(bot_key)

    @staticmethod
    def _event_value(event: Any, key: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(key, default)
        return getattr(event, key, default)

    @staticmethod
    def _normalize_qq(value: Any) -> str | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str):
            return None
        qq = value.strip()
        if (
            not qq
            or len(qq) > 20
            or not qq.isascii()
            or not qq.isdigit()
            or not qq.strip("0")
        ):
            return None
        return qq
