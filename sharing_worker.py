"""On-demand publisher events and snapshot uploads."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from .schema import StateError
from .sharing import (
    _DEFAULT_STATE,
    _MAX_PAYLOAD_BYTES,
    _TABLE,
    SharingExporter,
    SharingHTTPError,
    _account_row,
    _normalize_sections,
    _utc_iso,
)


class SharingManager:
    """Keep one SSE stream open and export only on a refresh event."""

    def __init__(
        self,
        engine,
        *,
        clock=None,
        session_factory=None,
        interval: float | None = None,
        request_timeout: float = 8.0,
    ):
        self.engine = engine
        self.exporter = SharingExporter(engine)
        self.clock = clock or engine.clock or time.time
        self.session_factory = session_factory
        self.request_timeout = max(0.1, float(request_timeout))
        self._task: asyncio.Task | None = None
        self._session = None
        self._wake = asyncio.Event()
        self._api_lock = asyncio.Lock()
        self._closed = False
        self._revision = 0
        with self.engine.transaction():
            self.engine.db.execute(
                f"CREATE TABLE IF NOT EXISTS {_TABLE} "
                "(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)"
            )
            self.engine.db.execute(
                f"INSERT OR IGNORE INTO {_TABLE}(id,body) VALUES (1,?)",
                (json.dumps(_DEFAULT_STATE, ensure_ascii=False),),
            )
        self._needs_status_check = bool(self._read_state()["connected"])

    def _read_state(self) -> dict[str, Any]:
        with self.engine.lock:
            row = self.engine.db.execute(
                f"SELECT body FROM {_TABLE} WHERE id=1"
            ).fetchone()
            if row is None:
                return dict(_DEFAULT_STATE)
            try:
                parsed = json.loads(row["body"])
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = {}
            state = dict(_DEFAULT_STATE)
            if isinstance(parsed, dict):
                state.update(parsed)
            state.pop("latest_snapshot", None)
            return state

    def _write_state(self, state: dict[str, Any]) -> None:
        with self.engine.transaction():
            self.engine.db.execute(
                f"INSERT INTO {_TABLE}(id,body) VALUES (1,?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (json.dumps(state, ensure_ascii=False, separators=(",", ":")),),
            )

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        if state["pending_disable"]:
            label = "pending_disable"
        elif not state["connected"]:
            label = "disconnected"
        elif state["last_error"]:
            label = "error"
        elif state["enabled"]:
            label = "sharing"
        else:
            label = "disabled"
        return {
            "connected": bool(state["connected"]),
            "enabled": bool(state["enabled"]),
            "account_id": str(state["account_id"] or ""),
            "server_url": str(state["server_url"] or ""),
            "view_url": str(state["view_url"] or ""),
            "sections": _normalize_sections(state.get("sections", {})),
            "last_success": state["last_success"],
            "last_error": str(state["last_error"] or ""),
            "remote_public_enabled": state["remote_public_enabled"],
            "pending_disable": bool(state["pending_disable"]),
            "status": label,
        }

    async def _new_session(self):
        if self.session_factory is not None:
            session = self.session_factory()
            if hasattr(session, "__await__"):
                session = await session
            return session
        try:
            import aiohttp
        except ImportError as exc:
            raise StateError("分享网络组件不可用") from exc
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.request_timeout),
            trust_env=False,
            json_serialize=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    async def start(self) -> None:
        if self._closed:
            raise StateError("分享服务实例已关闭")
        if self._task is not None and not self._task.done():
            return
        if self._session is None:
            self._session = await self._new_session()
        self._task = asyncio.create_task(self._run(), name="better-real-me-sharing")
        self._wake.set()

    async def _stop_worker(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def close(self) -> None:
        """Stop network work while preserving credentials needed for revoke retry."""
        async with self._api_lock:
            if self._closed:
                return
            self._closed = True
            self._revision += 1
            await self._stop_worker()
            session, self._session = self._session, None
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        token: str = "",
    ) -> dict[str, Any]:
        if self._session is None:
            raise StateError("分享网络组件不可用")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._session.request(
                method,
                url,
                json=payload,
                headers=headers,
                allow_redirects=False,
            )
            async with response:
                status_code = int(response.status)
                body = await response.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise StateError("无法连接分享服务") from exc
        return self._decode_json(status_code, body)

    @staticmethod
    def _decode_json(status_code: int, body: bytes) -> dict[str, Any]:
        if len(body) > _MAX_PAYLOAD_BYTES:
            raise StateError("分享服务响应超过大小限制")
        if status_code < 200 or status_code >= 300:
            raise SharingHTTPError(status_code)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateError("分享服务返回格式无效") from exc
        if not isinstance(parsed, dict):
            raise StateError("分享服务返回格式无效")
        return parsed

    async def _request_info(self, url: str, *, redirected: bool = False):
        """Probe info without ever forwarding a ticket or Authorization header."""
        try:
            response = await self._session.request(
                "GET",
                url,
                headers={"Accept": "application/json"},
                allow_redirects=False,
            )
            async with response:
                status_code = int(response.status)
                location = getattr(response, "headers", {}).get("Location")
                body = await response.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise StateError("无法连接分享服务") from exc

        if status_code in {301, 302, 303, 307, 308} and location and not redirected:
            target = urlsplit(urljoin(url, location))
            source = urlsplit(url)
            if (
                target.scheme == "https"
                and target.hostname
                and source.hostname
                and target.hostname.casefold() == source.hostname.casefold()
                and not target.username
                and not target.password
                and not target.query
                and not target.fragment
            ):
                return await self._request_info(
                    urlunsplit((target.scheme, target.netloc, target.path, "", "")),
                    redirected=True,
                )
        return self._decode_json(status_code, body)

    @staticmethod
    def _binding(binding_url: Any) -> tuple[str, str]:
        if not isinstance(binding_url, str) or len(binding_url) > 4096:
            raise StateError("绑定链接格式无效")
        try:
            parsed = urlsplit(binding_url.strip())
            fragment = parse_qs(parsed.fragment, strict_parsing=True)
            tickets = fragment.get("bind", [])
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            raise StateError("绑定链接格式无效") from None
        if (
            parsed.scheme not in {"https", "http"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or len(tickets) != 1
            or not tickets[0]
            or len(tickets[0]) > 1024
        ):
            raise StateError("绑定链接格式无效")
        netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            netloc = f"[{hostname}]"
        if port is not None:
            netloc = f"{netloc}:{port}"
        base_url = urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))
        return base_url, tickets[0]

    @staticmethod
    def _endpoint(server_url: str, path: str) -> str:
        return f"{server_url.rstrip('/')}/{path.lstrip('/')}"

    def _canonical_base(self, requested: str, info: dict[str, Any]) -> str:
        if info.get("refresh_mode") != "on_request":
            raise StateError("分享服务不支持访问时刷新，请更新分享服务后重试")
        public_url = info.get("public_url")
        if not isinstance(public_url, str):
            raise StateError("分享服务未返回规范地址")
        original = urlsplit(requested)
        canonical = urlsplit(public_url)
        if (
            canonical.scheme not in {"https", "http"}
            or not canonical.hostname
            or canonical.username is not None
            or canonical.password is not None
            or canonical.query
            or canonical.fragment
            or not original.hostname
            or canonical.hostname.casefold() != original.hostname.casefold()
        ):
            raise StateError("分享服务规范地址与绑定链接不匹配")
        if original.scheme == "https" and canonical.scheme != "https":
            raise StateError("分享服务规范地址未启用 HTTPS")

        base_path = info.get("base_path")
        path = canonical.path
        if isinstance(base_path, str) and base_path.startswith("/") and base_path != "/":
            path = base_path
        return urlunsplit(
            (canonical.scheme, canonical.netloc, path.rstrip("/"), "", "")
        )

    async def _probe_server(self, server_url: str) -> str:
        info = await self._request_info(
            self._endpoint(server_url, "/api/v1/info")
        )
        return self._canonical_base(server_url, info)

    async def _ensure_canonical_server(self, state: dict[str, Any]) -> dict[str, Any]:
        requested = str(state["server_url"] or "")
        canonical = await self._probe_server(requested)
        if canonical != requested:
            latest = self._read_state()
            if latest["server_url"] == requested:
                latest["server_url"] = canonical
                self._write_state(latest)
            state["server_url"] = canonical
        return state

    def _record_error(self, message: str) -> None:
        state = self._read_state()
        state["last_error"] = message
        self._write_state(state)

    def _select_account(self, account_id: Any) -> str:
        if account_id:
            with self.engine.lock:
                return _account_row(self.engine, account_id)["id"]
        with self.engine.lock:
            rows = self.engine.db.execute("SELECT id FROM accounts ORDER BY id").fetchall()
            verified = []
            for row in rows:
                try:
                    verified.append(_account_row(self.engine, row["id"])["id"])
                except StateError:
                    continue
        verified = list(dict.fromkeys(verified))
        if not verified:
            raise StateError("尚未发现真实 QQ 账号，请连接 QQ 后重试")
        if len(verified) > 1:
            raise StateError("检测到多个真实 QQ 账号，请选择一个")
        return verified[0]

    async def connect(
        self,
        binding_url: str,
        account_id: str | None = None,
        sections: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        async with self._api_lock:
            await self.start()
            state = self._read_state()
            if state["pending_disable"]:
                raise StateError("上一次远端关闭仍在重试，请稍后再连接")
            if state["connected"]:
                raise StateError("请先断开当前分享连接")

            selected = self._select_account(account_id)
            sections = _normalize_sections(
                sections
                if sections is not None
                else {"states": False, "schedule": False, "location": False}
            )
            requested_url, ticket = self._binding(binding_url)
            try:
                server_url = await self._probe_server(requested_url)
                result = await self._request_json(
                    "POST",
                    self._endpoint(server_url, "/api/v1/bind"),
                    payload={"ticket": ticket, "agent_version": "1.0.0"},
                )
            except StateError as exc:
                self._record_error("绑定分享服务失败")
                raise StateError("绑定分享服务失败，请检查链接后重试") from exc

            connection_id = result.get("connection_id")
            token = result.get("publisher_token")
            view_url = result.get("view_url")
            if (
                result.get("protocol") != 1
                or not isinstance(connection_id, str)
                or not connection_id
                or not isinstance(token, str)
                or not token
                or not isinstance(view_url, str)
                or not view_url
            ):
                self._record_error("分享服务绑定响应无效")
                raise StateError("分享服务绑定响应无效")

            state = self._read_state()
            state.update(
                connected=True,
                enabled=True,
                account_id=selected,
                server_url=server_url,
                view_url=view_url,
                connection_id=connection_id,
                publisher_token=token,
                sections=sections,
                last_error="",
                remote_public_enabled=False,
                pending_disable=False,
                enable_remote_pending=True,
                pending_settings=None,
                remove_after_revoke=False,
            )
            self._write_state(state)
            self._revision += 1
            self._needs_status_check = False
            self._wake.set()
            return self.status()

    async def update_settings(
        self,
        enabled: bool,
        account_id: str | None,
        sections: dict[str, bool],
    ) -> dict[str, Any]:
        async with self._api_lock:
            if not isinstance(enabled, bool):
                raise StateError("分享启用状态必须为布尔值")
            current = self._read_state()
            account_id = self._select_account(
                account_id or (current["account_id"] if current["connected"] else None)
            )
            sections = _normalize_sections(sections)
            await self.start()
            state = self._read_state()
            if enabled and not state["connected"]:
                raise StateError("请先连接分享服务")

            desired = {
                "account_id": account_id,
                "sections": sections,
                "enabled": enabled,
                "open_remote": enabled,
            }
            if state["pending_disable"]:
                await self._stop_worker()
                state["pending_settings"] = desired
                state["remove_after_revoke"] = False
                self._write_state(state)
                self._revision += 1
                await self._try_revoke()
                state = self._read_state()
                if state["pending_disable"] or state["enabled"]:
                    self._wake.set()
                    await self.start()
                return self.status()

            account_changed = state["connected"] and state["account_id"] != account_id
            sections_changed = state["sections"] != sections
            must_revoke = bool(
                state["connected"]
                and state["remote_public_enabled"] is not False
                and (account_changed or sections_changed or not enabled)
            )
            if must_revoke:
                await self._stop_worker()
                state = self._read_state()
                state["enabled"] = False
                state["pending_disable"] = True
                state["enable_remote_pending"] = False
                state["pending_settings"] = desired
                state["remove_after_revoke"] = False
                state["last_error"] = ""
                self._write_state(state)
                self._revision += 1
                await self._try_revoke()
                state = self._read_state()
                if state["pending_disable"] or state["enabled"]:
                    self._wake.set()
                    await self.start()
                return self.status()

            state["account_id"] = account_id
            state["sections"] = sections
            state["enabled"] = enabled
            if enabled and not current["enabled"]:
                state["enable_remote_pending"] = True
                state["last_error"] = ""
            elif not enabled:
                state["enable_remote_pending"] = False
            self._write_state(state)
            self._revision += 1
            if enabled:
                self._wake.set()
            return self.status()

    async def disconnect(self) -> dict[str, Any]:
        async with self._api_lock:
            await self.start()
            await self._stop_worker()
            state = self._read_state()
            if not state["connected"]:
                state["enabled"] = False
                self._write_state(state)
                return self.status()
            state["enabled"] = False
            state["enable_remote_pending"] = False
            state["pending_settings"] = None
            state["remove_after_revoke"] = True
            state["pending_disable"] = True
            state["last_error"] = ""
            self._write_state(state)
            self._revision += 1
            await self._try_revoke()
            state = self._read_state()
            if state["pending_disable"]:
                self._wake.set()
                await self.start()
            return self.status()

    async def _try_revoke(self) -> bool:
        state = self._read_state()
        if not state["connected"] or not state["publisher_token"]:
            state["pending_disable"] = False
            self._write_state(state)
            return True
        try:
            state = await self._ensure_canonical_server(state)
            result = await self._request_json(
                "POST",
                self._endpoint(state["server_url"], "/api/v1/publisher/visibility"),
                payload={"enabled": False},
                token=state["publisher_token"],
            )
        except SharingHTTPError as exc:
            if exc.status_code in {401, 404}:
                self._clear_invalid_authorization()
                return False
            return self._mark_revoke_pending()
        except StateError:
            return self._mark_revoke_pending()

        remote_enabled = result.get("public_enabled")
        if not isinstance(remote_enabled, bool):
            return self._mark_revoke_pending()
        state = self._read_state()
        state["remote_public_enabled"] = remote_enabled
        state["pending_disable"] = remote_enabled
        if state["pending_disable"]:
            state["last_error"] = "远端公开关闭待重试"
        elif state["remove_after_revoke"]:
            self._clear_connection(state)
        elif state["pending_settings"]:
            self._apply_pending_settings(state)
        else:
            state["enabled"] = False
            state["last_error"] = ""
        self._write_state(state)
        return not state["pending_disable"]

    def _mark_revoke_pending(self) -> bool:
        state = self._read_state()
        state["last_error"] = "远端公开关闭待重试"
        state["pending_disable"] = True
        self._write_state(state)
        return False

    @staticmethod
    def _clear_connection(state: dict[str, Any]) -> None:
        state["connected"] = False
        state["publisher_token"] = ""
        state["connection_id"] = ""
        state["server_url"] = ""
        state["view_url"] = ""
        state["remote_public_enabled"] = False
        state["pending_disable"] = False
        state["remove_after_revoke"] = False
        state["pending_settings"] = None
        state["enable_remote_pending"] = False
        state["last_error"] = ""

    def _clear_invalid_authorization(self) -> None:
        state = self._read_state()
        pending = state.get("pending_settings")
        state["connected"] = False
        state["enabled"] = False
        state["publisher_token"] = ""
        state["connection_id"] = ""
        state["server_url"] = ""
        state["view_url"] = ""
        state["remote_public_enabled"] = None
        state["pending_disable"] = False
        state["enable_remote_pending"] = False
        state["remove_after_revoke"] = False
        if isinstance(pending, dict):
            state["account_id"] = pending.get("account_id", state["account_id"])
            state["sections"] = pending.get("sections", state["sections"])
        state["pending_settings"] = None
        state["last_error"] = "旧授权已失效，请在公网管理端确认旧链接"
        self._write_state(state)

    @staticmethod
    def _apply_pending_settings(state: dict[str, Any]) -> None:
        desired = state.get("pending_settings")
        if not isinstance(desired, dict):
            return
        state["account_id"] = desired["account_id"]
        state["sections"] = desired["sections"]
        state["enabled"] = bool(desired["enabled"])
        state["pending_settings"] = None
        state["pending_disable"] = False
        state["enable_remote_pending"] = bool(desired.get("open_remote"))
        state["last_error"] = ""

    def _persist_sequence(
        self, snapshot: dict[str, Any], account_id: str, revision: int
    ):
        body = json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(body) > _MAX_PAYLOAD_BYTES:
            raise StateError("分享快照超过 2 MiB 限制")
        state = self._read_state()
        if (
            revision != self._revision
            or not state["connected"]
            or not state["enabled"]
            or state["pending_disable"]
            or state["account_id"] != account_id
        ):
            return None
        state["sequence"] = max(0, int(state.get("sequence", 0))) + 1
        snapshot["sequence"] = state["sequence"]
        self._write_state(state)
        return state

    async def _publish_for_refresh(self) -> None:
        async with self._api_lock:
            state = self._read_state()
            if (
                not state["connected"]
                or not state["enabled"]
                or state["pending_disable"]
            ):
                return
            revision = self._revision
            account_id = state["account_id"]
            sequence = max(0, int(state.get("sequence", 0))) + 1
            snapshot = await asyncio.to_thread(
                self.exporter.build_snapshot,
                account_id,
                state["sections"],
                sequence,
                self.clock(),
            )
            persisted = self._persist_sequence(snapshot, account_id, revision)
            if persisted is None:
                return
            try:
                result = await self._request_json(
                    "POST",
                    self._endpoint(
                        persisted["server_url"], "/api/v1/publisher/snapshot"
                    ),
                    payload=snapshot,
                    token=persisted["publisher_token"],
                )
            except SharingHTTPError as exc:
                if exc.status_code in {401, 404}:
                    self._clear_invalid_authorization()
                raise
            if result.get("accepted") is not True:
                raise StateError("分享服务未接收最新快照")
            state = self._read_state()
            if revision != self._revision or not state["enabled"]:
                return
            state["last_success"] = str(
                result.get("received_at") or _utc_iso(self.clock())
            )
            state["last_error"] = ""
            if isinstance(result.get("view_url"), str):
                state["view_url"] = result["view_url"]
            self._write_state(state)

    @staticmethod
    def _sse_timeout():
        try:
            import aiohttp
        except ImportError:
            return None
        return aiohttp.ClientTimeout(total=None, sock_read=None, connect=8)

    async def _listen_once(self, expected: dict[str, Any]) -> None:
        state = await self._ensure_canonical_server(expected)
        kwargs = {
            "headers": {
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {state['publisher_token']}",
            },
            "allow_redirects": False,
        }
        timeout = self._sse_timeout()
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = await self._session.get(
                self._endpoint(state["server_url"], "/api/v1/publisher/events"),
                **kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise StateError("无法连接分享服务事件流") from exc

        async with response:
            status_code = int(response.status)
            if status_code in {401, 404}:
                self._clear_invalid_authorization()
                return
            if status_code < 200 or status_code >= 300:
                raise SharingHTTPError(status_code)

            async with self._api_lock:
                state = self._read_state()
                if (
                    not state["connected"]
                    or not state["enabled"]
                    or state["pending_disable"]
                    or state["publisher_token"] != expected["publisher_token"]
                ):
                    return
                if state["enable_remote_pending"]:
                    try:
                        result = await self._request_json(
                            "POST",
                            self._endpoint(
                                state["server_url"], "/api/v1/publisher/visibility"
                            ),
                            payload={"enabled": True},
                            token=state["publisher_token"],
                        )
                    except SharingHTTPError as exc:
                        if exc.status_code in {401, 404}:
                            self._clear_invalid_authorization()
                        raise
                    if result.get("public_enabled") is not True:
                        state["enabled"] = False
                        state["enable_remote_pending"] = False
                        state["remote_public_enabled"] = False
                        state["last_error"] = "分享服务未确认公开状态已开启"
                        self._write_state(state)
                        return
                    state["remote_public_enabled"] = True
                    state["enable_remote_pending"] = False
                    state["last_error"] = ""
                    self._write_state(state)
                    self._needs_status_check = False
                elif self._needs_status_check:
                    try:
                        result = await self._request_json(
                            "GET",
                            self._endpoint(
                                state["server_url"], "/api/v1/publisher/status"
                            ),
                            token=state["publisher_token"],
                        )
                    except SharingHTTPError as exc:
                        if exc.status_code in {401, 404}:
                            self._clear_invalid_authorization()
                        raise
                    if not isinstance(result.get("public_enabled"), bool):
                        raise StateError("分享服务状态响应无效")
                    if result["public_enabled"] is False:
                        self._remote_visibility_closed()
                        return
                    state["remote_public_enabled"] = True
                    if isinstance(result.get("view_url"), str):
                        state["view_url"] = result["view_url"]
                    self._write_state(state)
                    self._needs_status_check = False

            while True:
                event, data = await self._read_sse_event(response.content)
                if event is None:
                    return
                if event == "refresh":
                    await self._publish_for_refresh()
                elif event == "visibility":
                    try:
                        payload = json.loads(data or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict) and payload.get("enabled") is False:
                        self._remote_visibility_closed()
                        return

    @staticmethod
    async def _read_sse_event(content):
        event = "message"
        data: list[str] = []
        while True:
            line = await content.readline()
            if not line:
                return None, ""
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            line = line.rstrip("\r\n")
            if not line:
                if data or event != "message":
                    return event, "\n".join(data)
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if not separator:
                value = ""
            elif value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value or "message"
            elif field == "data":
                data.append(value)

    def _remote_visibility_closed(self) -> None:
        state = self._read_state()
        state["enabled"] = False
        state["remote_public_enabled"] = False
        state["enable_remote_pending"] = False
        state["pending_disable"] = False
        state["last_error"] = "分享服务端已关闭公开状态，本地已暂停"
        self._write_state(state)
        self._revision += 1

    async def _run(self) -> None:
        retry_delay = 1.0
        while not self._closed:
            self._wake.clear()
            state = self._read_state()
            if state["pending_disable"]:
                if await self._try_revoke():
                    retry_delay = 1.0
                    continue
                await self._wait_retry(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)
                continue
            if not state["connected"] or not state["enabled"]:
                retry_delay = 1.0
                await self._wake.wait()
                continue
            try:
                await self._listen_once(state)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if self._read_state()["connected"]:
                    self._record_error("分享服务暂时无法连接，正在重试")
            if self._closed:
                return
            state = self._read_state()
            if state["pending_disable"] or not state["connected"] or not state["enabled"]:
                retry_delay = 1.0
                continue
            await self._wait_retry(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    async def _wait_retry(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except TimeoutError:
            pass
