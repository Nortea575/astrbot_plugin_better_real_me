"""AstrBot View, message commands, and LLM hooks for Better Real Me."""

from __future__ import annotations

import inspect
import sys
import uuid
from collections.abc import Mapping

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart

from .commands import (
    exact_query,
    help_text,
    query_usage,
    render_location,
    render_schedule,
    render_states,
    resolve_mode,
)
from .engine import Engine, dump, real_actor_context, real_actor_identity
from .life_runner import LifeRunner
from .memory_bridge import LifeMemoryBridge
from .presence import QQPresence
from .schema import StateError
from .sharing import SharingManager

_VERSION = "1.0.0"


@register(
    "astrbot_plugin_better_real_me",
    "Xing",
    "状态、日程与角色的持续生活模拟",
    _VERSION,
)
class BetterRealMe(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._terminated = False
        self.context = context
        self.engine = Engine(StarTools.get_data_dir() / "states.sqlite3")
        self.life = self.engine.life
        self.presence = QQPresence(context, self.engine.record_account)
        self.memory_bridge = LifeMemoryBridge(context, self.life, self.engine)
        self.life_runner = LifeRunner(self.life, context, self.memory_bridge)
        self.sharing = SharingManager(self.engine)
        for endpoint, methods, handler in [
            ("bootstrap", ["GET"], self.bootstrap),
            ("sharing/status", ["GET"], self.sharing_status),
            ("sharing/connect", ["POST"], self.sharing_connect),
            ("sharing/settings", ["POST"], self.sharing_settings),
            ("sharing/disconnect", ["POST"], self.sharing_disconnect),
            ("states/save", ["POST"], self.save_state),
            ("states/delete", ["POST"], self.delete_state),
            ("groups/save", ["POST"], self.save_group),
            ("groups/delete", ["POST"], self.delete_group),
            ("schedule/save", ["POST"], self.save_schedule),
            ("indoor/save", ["POST"], self.save_indoor),
            ("indoor/route", ["POST"], self.route_indoor),
            ("indoor/move", ["POST"], self.move_indoor),
            ("categories", ["POST"], self.category),
            ("order", ["POST"], self.reorder),
            ("runtime", ["POST"], self.adjust),
            ("interact", ["POST"], self.interact),
            ("life/preset", ["POST"], self.life_preset),
            ("life/settings", ["POST"], self.life_settings),
            ("life/actions/save", ["POST"], self.life_save_action),
            ("life/actions/delete", ["POST"], self.life_delete_action),
        ]:
            context.register_web_api(
                f"/astrbot_plugin_better_real_me/{endpoint}",
                self._wrap(handler),
                methods,
                "Better Real Me " + endpoint,
            )

    @staticmethod
    def _wrap(handler):
        owner = getattr(handler, "__self__", None)

        async def wrapped():
            if owner is not None and getattr(owner, "_terminated", False):
                return error_response(
                    "插件实例已停用，当前请求不可用。", status_code=503
                )
            try:
                return json_response(await handler())
            except (StateError, ValueError, TypeError, KeyError) as exc:
                return error_response(str(exc), status_code=400)

        return wrapped

    @staticmethod
    async def _body():
        body = await request.json(default={})
        if not isinstance(body, dict):
            raise StateError("请求内容必须为对象")
        return body

    def _bootstrap(self, actor):
        result = self.engine.overview(actor)
        result["preview"] = (
            self.engine.describe(self.engine.snapshot(result["context"]))
            if result["context"]
            else ""
        )
        life_api = getattr(self, "life", getattr(self.engine, "life", None))
        getter = getattr(life_api, "get_overview", None)
        life = getter() if callable(getter) else result.get("life", {})
        if not isinstance(life, dict):
            life = {}
        options = life.setdefault("options", {})
        if not isinstance(options, dict):
            options = {}
            life["options"] = options
        options["personas"] = self._persona_options()
        options["providers"] = self._provider_options()
        integrations = life.setdefault("integrations", {})
        if not isinstance(integrations, dict):
            integrations = {}
            life["integrations"] = integrations
        bridge = getattr(self, "memory_bridge", None)
        status = getattr(bridge, "status", None)
        integrations["livingmemory"] = (
            status()
            if callable(status)
            else {"installed": False, "ready": False, "version": ""}
        )
        actor_character = (
            str(actor.get("character") or "") if isinstance(actor, dict) else ""
        )
        if actor_character.startswith("life:"):
            selected_account = actor_character[len("life:") :]
            if any(
                isinstance(row, dict) and row.get("id") == selected_account
                for row in life.get("accounts", [])
            ):
                life["selected_account_id"] = selected_account
        result["life"] = life
        result["version"] = _VERSION
        sharing = getattr(self, "sharing", None)
        status = getattr(sharing, "status", None)
        if callable(status):
            result["sharing"] = status()
        return result

    async def _bootstrap_async(self, actor):
        result = self._bootstrap(actor)
        options = result["life"].setdefault("options", {})
        options["personas"] = await self._persona_options_async()
        options["providers"] = self._provider_options()
        return result

    def _persona_options(self):
        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        values = getattr(manager, "personas", None)
        rows = self._option_rows(values, ("persona_id", "id"), ("name", "persona_name"))
        if not any(row["id"] == "default" for row in rows):
            rows.insert(0, {"id": "default", "name": "默认人格"})
        return rows

    async def _persona_options_async(self):
        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        values = None
        getter = getattr(manager, "get_all_personas", None)
        if callable(getter):
            try:
                values = getter()
                if inspect.isawaitable(values):
                    values = await values
            except Exception:  # noqa: BLE001
                values = None
        if values is None:
            values = getattr(manager, "personas", None)
        rows = self._option_rows(values, ("persona_id", "id"), ("name", "persona_name"))
        if not any(row["id"] == "default" for row in rows):
            rows.insert(0, {"id": "default", "name": "默认人格"})
        default_id = getattr(manager, "default_persona", None)
        if (
            isinstance(default_id, str)
            and default_id.strip()
            and not any(row["id"] == default_id for row in rows)
        ):
            rows.append({"id": default_id, "name": default_id})
        return rows

    def _provider_options(self):
        context = getattr(self, "context", None)
        getter = getattr(context, "get_all_providers", None)
        try:
            values = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001
            values = None
        if inspect.isawaitable(values):
            close = getattr(values, "close", None)
            if callable(close):
                close()
            values = None
        if values is None:
            manager = getattr(context, "provider_manager", None)
            values = getattr(manager, "provider_insts", None)
        if not isinstance(values, (list, tuple, set)):
            return []
        rows = []
        seen = set()
        for provider in values:
            meta_method = getattr(provider, "meta", None)
            try:
                meta = meta_method() if callable(meta_method) else None
            except Exception:  # noqa: BLE001
                meta = None
            provider_id = getattr(meta, "id", None) or getattr(
                provider, "provider_id", None
            )
            if (
                not isinstance(provider_id, str)
                or not provider_id.strip()
                or provider_id in seen
            ):
                continue
            name = (
                getattr(meta, "model", None)
                or getattr(meta, "name", None)
                or getattr(provider, "provider_name", None)
                or provider_id
            )
            rows.append({"id": provider_id, "name": str(name)[:120]})
            seen.add(provider_id)
        return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))

    @staticmethod
    def _option_rows(values, id_fields, name_fields):
        if isinstance(values, dict):
            iterable = [(key, value) for key, value in values.items()]
        elif isinstance(values, (list, tuple, set)):
            iterable = [(None, value) for value in values]
        else:
            iterable = []
        rows = []
        seen = set()
        for key, item in iterable:
            identifier = None
            for field in id_fields:
                identifier = (
                    item.get(field)
                    if isinstance(item, dict)
                    else getattr(item, field, None)
                )
                if identifier:
                    break
            if not identifier:
                identifier = key
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in seen
            ):
                continue
            name = None
            for field in name_fields:
                name = (
                    item.get(field)
                    if isinstance(item, dict)
                    else getattr(item, field, None)
                )
                if name:
                    break
            rows.append({"id": identifier, "name": str(name or identifier)[:120]})
            seen.add(identifier)
        return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))

    def _verified_life_account(self, account_id):
        if not isinstance(account_id, str) or not account_id.strip():
            raise StateError("请选择已发现的真实 QQ 账号")
        accounts = self.life.get_overview().get("accounts", [])
        if not any(
            isinstance(row, dict) and row.get("id") == account_id for row in accounts
        ):
            raise StateError("QQ 账号未被插件确认")
        return account_id

    def _enabled_life_actor(self, account_id):
        is_enabled = getattr(self.life, "is_enabled", None)
        if callable(is_enabled) and not is_enabled(account_id):
            return None
        try:
            return self.life.actor(account_id)
        except StateError:
            # The account may have been paused between checking it and reading
            # the overview. Keep the selection metadata and fall back safely.
            return None

    async def _bootstrap_for_body(self, body):
        actor = self._view_context(body, body.get("context"))
        account_id = body.get("account_id")
        if account_id:
            account_id = self._verified_life_account(body["account_id"])
            actor = self._enabled_life_actor(account_id) or actor
        result = await self._bootstrap_async(actor)
        if account_id:
            result["life"]["selected_account_id"] = account_id
        return result

    def _validate_optional_life_account(self, body):
        if body.get("account_id"):
            return self._verified_life_account(body["account_id"])
        return None

    async def initialize(self):
        await self.presence.start()
        await self.life_runner.start()
        await self.sharing.start()

    async def sharing_status(self):
        return self.sharing.status()

    async def sharing_connect(self):
        body = await self._body()
        return await self.sharing.connect(
            body.get("binding_url"),
            body.get("account_id"),
            body.get("sections"),
        )

    async def sharing_settings(self):
        body = await self._body()
        return await self.sharing.update_settings(
            body.get("enabled"),
            body.get("account_id"),
            body.get("sections"),
        )

    async def sharing_disconnect(self):
        await self._body()
        return await self.sharing.disconnect()

    async def bootstrap(self):
        query_actor = {
            key: request.query.get(key, "")
            for key in ["character", "conversation", "user"]
        }
        account_id = request.query.get("account_id", "")
        if account_id:
            account_id = self._verified_life_account(account_id)
            query_actor = self._enabled_life_actor(account_id) or query_actor
        result = await self._bootstrap_async(query_actor)
        if account_id:
            result["life"]["selected_account_id"] = account_id
        return result

    async def save_state(self):
        body = await self._body()
        self._validate_optional_life_account(body)
        self.engine.save_state(body["definition"], creating=body.get("create", False))
        return await self._bootstrap_for_body(body)

    async def delete_state(self):
        body = await self._body()
        self._validate_optional_life_account(body)
        self.engine.delete_state(body["id"])
        return await self._bootstrap_for_body(body)

    async def save_group(self):
        body = await self._body()
        self._validate_optional_life_account(body)
        self.engine.save_group(
            body.get("group", body.get("definition")),
            creating=body.get("create", False),
        )
        return await self._bootstrap_for_body(body)

    async def delete_group(self):
        body = await self._body()
        self._validate_optional_life_account(body)
        self.engine.delete_group(body["id"])
        return await self._bootstrap_for_body(body)

    async def save_schedule(self):
        body = await self._body()
        self.engine.save_schedule(
            body["schedule"], category_changes=body.get("category_changes")
        )
        return await self._bootstrap_for_body(body)

    async def save_indoor(self):
        body = await self._body()
        self.engine.save_indoor(body["indoor"], body.get("relocate"))
        return await self._bootstrap_for_body(body)

    async def route_indoor(self):
        body = await self._body()
        actor = self._request_actor(body)
        return self.engine.route_indoor(actor, body["room_id"])

    async def move_indoor(self):
        body = await self._body()
        actor = self._request_actor(body)
        result = self.engine.move_indoor(actor, body["room_id"], body.get("token", ""))
        overview = await self._bootstrap_for_body({**body, "context": actor})
        overview["move"] = result["move"]
        return overview

    async def category(self):
        body = await self._body()
        self.engine.category(body["action"], body.get("id", ""), body.get("name", ""))
        return await self._bootstrap_for_body(body)

    async def reorder(self):
        body = await self._body()
        self.engine.reorder(body["ids"], body.get("categories", {}))
        return await self._bootstrap_for_body(body)

    async def adjust(self):
        body = await self._body()
        if body.get("account_id"):
            actor = self.life.actor(self._verified_life_account(body["account_id"]))
            self.life.adjust_state(
                body["account_id"],
                body["id"],
                body.get("value"),
                body.get("reset", False),
            )
        else:
            actor = self.engine.registered_context(body.get("context"))
            self.engine.adjust(
                body["id"], actor, body.get("value"), body.get("reset", False)
            )
        return await self._bootstrap_for_body({**body, "context": actor})

    def _request_actor(self, body):
        if body.get("account_id"):
            return self.life.actor(self._verified_life_account(body["account_id"]))
        return self.engine.registered_context(body.get("context"))

    def _view_context(self, body, fallback):
        raw = body.get("view_context")
        if raw is None:
            return fallback
        try:
            return self.engine.registered_context(raw)
        except StateError:
            return fallback

    async def interact(self):
        body = await self._body()
        if body.get("account_id"):
            account_id = self._verified_life_account(body["account_id"])
            if body.get("state_id") not in self.life.owned_state_ids():
                raise StateError("只能通过QQ账号操作生活内核拥有的状态")
            actor = self.life.actor(account_id)
        else:
            actor = self.engine.registered_context(body.get("context"))
        result = self.engine.execute(
            body["state_id"],
            body["interaction_id"],
            actor,
            token=body.get("token", ""),
        )
        overview = await self._bootstrap_for_body({**body, "context": actor})
        overview.update(
            ok=result["ok"], message=result["message"], duplicate=result["duplicate"]
        )
        return overview

    async def life_preset(self):
        body = await self._body()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise StateError("生活预设启用状态必须为布尔值")
        preset_id = body.get("preset_id")
        persona_id = body.get("persona_id", "")
        if not isinstance(preset_id, str) or not preset_id.strip():
            raise StateError("请选择生活预设")
        if not isinstance(persona_id, str):
            raise StateError("人格标识无效")
        account_id = self._verified_life_account(body.get("account_id"))
        persona_id = await self._resolve_life_persona(account_id, persona_id.strip())
        self.life.configure_preset(
            preset_id.strip(),
            enabled,
            account_id,
            persona_id,
        )
        return await self._bootstrap_for_body(body)

    async def _resolve_life_persona(self, account_id, requested):
        options = await self._persona_options_async()
        valid_ids = {item["id"] for item in options}
        if requested:
            if requested not in valid_ids:
                raise StateError("请选择当前可用的人格")
            return requested

        accounts = self.life.get_overview().get("accounts", [])
        account = next(
            (
                row
                for row in accounts
                if isinstance(row, dict) and row.get("id") == account_id
            ),
            {},
        )
        existing = str(account.get("persona_id") or "").strip()
        if existing in valid_ids:
            return existing

        identity = real_actor_identity(account.get("context"))
        platform, _, qq = account_id.partition(":")
        if identity and identity[:2] == (platform, qq) and identity[2] in valid_ids:
            return identity[2]

        manager = getattr(getattr(self, "context", None), "persona_manager", None)
        default_id = getattr(manager, "default_persona", "")
        if isinstance(default_id, str) and default_id in valid_ids:
            return default_id
        if "default" in valid_ids:
            return "default"
        raise StateError("没有可用人格，请先在 AstrBot 中配置人格")

    async def life_settings(self):
        body = await self._body()
        account_id = self._verified_life_account(body.get("account_id"))
        settings = {}
        for key in ("memory_enabled", "model_enabled"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise StateError(f"{key} 必须为布尔值")
                settings[key] = body[key]
        if "provider_id" in body:
            provider_id = body["provider_id"]
            if not isinstance(provider_id, str):
                raise StateError("provider_id 必须为字符串")
            settings["provider_id"] = provider_id.strip()
        if not settings:
            raise StateError("没有可保存的生活设置")
        self.life.update_settings(account_id, **settings)
        return await self._bootstrap_for_body(body)

    async def life_save_action(self):
        body = await self._body()
        self._validate_optional_life_account(body)
        self.life.save_action(body["action"])
        return await self._bootstrap_for_body(body)

    async def life_delete_action(self):
        body = await self._body()
        self._validate_optional_life_account(body)
        action_id = body.get("id")
        if not isinstance(action_id, str) or not action_id:
            raise StateError("缺少生活动作标识")
        self.life.delete_action(action_id)
        return await self._bootstrap_for_body(body)

    async def _actor(self, event: AstrMessageEvent, req=None):
        cached = event.get_extra("brm_actor")
        try:
            self_id = str(event.get_self_id() or "").strip()
            sender_id = str(event.get_sender_id() or "").strip()
            platform_id = str(event.get_platform_id() or "").strip()
        except (AttributeError, TypeError):
            return None
        if (
            not platform_id
            or not self_id.isascii()
            or not self_id.isdigit()
            or self_id == "0"
            or not sender_id.isascii()
            or not sender_id.isdigit()
            or sender_id == "0"
        ):
            return None
        try:
            self.engine.record_account(platform_id, self_id)
        except StateError:
            return None
        canonical_cached = real_actor_context(cached)
        identity = real_actor_identity(canonical_cached)
        if (
            req is None
            and canonical_cached is not None
            and identity is not None
            and identity[:2] == (platform_id, self_id)
            and canonical_cached["user"] == f"{platform_id}:{self_id}:{sender_id}"
        ):
            return canonical_cached
        umo = str(event.unified_msg_origin)
        conversation = getattr(req, "conversation", None)
        if conversation is None:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if cid:
                conversation = await manager.get_conversation(umo, cid)
        selected, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=getattr(conversation, "persona_id", None),
            platform_name=event.get_platform_name(),
            provider_settings=self.context.get_config(umo).get("provider_settings", {}),
        )
        prefix = f"{platform_id}:{self_id}"
        actor = real_actor_context(
            {
                "character": f"{prefix}:{str(selected or 'default').strip()}",
                "conversation": umo,
                "user": f"{prefix}:{sender_id}",
            }
        )
        if actor is None:
            return None
        event.set_extra("brm_actor", actor)
        return actor

    @staticmethod
    def _token(event):
        token = event.get_extra("brm_turn")
        if not token:
            token = str(
                getattr(event.message_obj, "message_id", "") or uuid.uuid4().hex
            )
            event.set_extra("brm_turn", token)
        return token

    def _wake_prefix(self, event):
        raw = getattr(getattr(event, "message_obj", None), "message_str", "")
        message_getter = getattr(event, "get_message_str", None)
        current = (
            message_getter()
            if callable(message_getter)
            else getattr(event, "message_str", "")
        )
        raw = str(raw or "").strip()
        current = str(current or "").strip()

        config = {}
        getter = getattr(getattr(self, "context", None), "get_config", None)
        if callable(getter):
            try:
                config = getter(getattr(event, "unified_msg_origin", ""))
            except Exception:  # noqa: BLE001
                config = {}
        configured = (
            config.get("wake_prefix", []) if isinstance(config, Mapping) else []
        )
        if isinstance(configured, str):
            prefixes = [configured]
        elif isinstance(configured, (list, tuple)):
            prefixes = [item for item in configured if isinstance(item, str)]
        else:
            prefixes = []

        if raw == current:
            return ""
        for prefix in prefixes:
            if (
                prefix
                and raw.startswith(prefix)
                and raw[len(prefix) :].strip() == current
            ):
                return prefix
        if current and raw.endswith(current):
            leading = raw[: -len(current)].rstrip()
            if leading in prefixes:
                return leading
        return next((item for item in prefixes if item), "")

    def _query_mode(self, actor):
        account_getter = getattr(self.life, "account_for_actor", None)
        if not callable(account_getter):
            return {}
        account_id = account_getter(actor)
        if not account_id:
            return {}
        overview = self.life.get_overview()
        return resolve_mode(
            overview,
            account_id,
            life_state_ids=self.life.owned_state_ids(),
            life_group_ids=self.life.owned_group_ids(),
        )

    @staticmethod
    def _active_balance_help(event):
        handlers = event.get_extra("activated_handlers", [])
        for handler in handlers if isinstance(handlers, (list, tuple)) else []:
            if getattr(handler, "handler_name", "") != "show_help":
                continue
            module_path = str(getattr(handler, "handler_module_path", ""))
            if "astrbot_plugin_better_balance" not in module_path:
                continue
            try:
                from astrbot.core.star.star import star_map

                metadata = star_map.get(module_path)
                if metadata is None:
                    return True, None
                if getattr(metadata, "name", "") != "astrbot_plugin_better_balance":
                    return True, None
                plugin = getattr(metadata, "star_cls", None)
                formatter = getattr(plugin, "_help_text", None)
                store = getattr(plugin, "store", None)
                config_getter = getattr(store, "get_config", None)
                if not callable(formatter) or not callable(config_getter):
                    return True, None
                return True, str(formatter(config_getter(), event) or "")
            except Exception:  # noqa: BLE001
                return True, None
        return False, None

    @staticmethod
    def _query_message(event):
        getter = getattr(event, "get_message_str", None)
        return getter() if callable(getter) else getattr(event, "message_str", "")

    def _has_active_query_subcommand(self, event):
        handlers = event.get_extra("activated_handlers", [])
        if not isinstance(handlers, (list, tuple)):
            return False
        module_path = self.__class__.__module__
        return any(
            getattr(handler, "handler_module_path", "") == module_path
            and getattr(handler, "handler_name", "")
            in {"query_status", "query_schedule", "query_location"}
            for handler in handlers
        )

    def _reply_query_usage(self, event):
        event.stop_event()
        return event.plain_result(query_usage(self._wake_prefix(event)))

    @filter.on_llm_request()
    async def inject_states(self, event: AstrMessageEvent, req: ProviderRequest):
        actor = await self._actor(event, req)
        if actor is None:
            return
        snapshot = self.engine.snapshot(actor)
        prompt = self.engine.describe(snapshot)
        req.extra_user_content_parts = [
            part
            for part in req.extra_user_content_parts
            if not (
                part.get("text", "")
                if isinstance(part, dict)
                else getattr(part, "text", "")
            ).startswith("<better_real_me>")
        ]
        if prompt:
            req.extra_user_content_parts.append(TextPart(text=prompt).mark_as_temp())

    @filter.llm_tool(name="brm_read_state")
    async def read_state(self, event: AstrMessageEvent, state_id: str = "") -> str:
        """查询角色当前的生活状态及可用交互。状态由插件维护。

        Args:
            state_id(string): 状态标识；空字符串表示读取全部已启用状态。
        """
        actor = await self._actor(event)
        if actor is None:
            return dump({"states": []})
        snapshot = self.engine.snapshot(actor)
        states = self.engine.public(snapshot)
        return dump(
            {"states": [s for s in states if not state_id or s["id"] == state_id]}
        )

    @filter.llm_tool(name="brm_read_schedule")
    async def read_schedule(self, event: AstrMessageEvent) -> str:
        """查询当前和下一条已启用日程及其本地时间。"""
        actor = await self._actor(event)
        if actor is None:
            return dump({"error": "无法确认真实 QQ 身份，未记录状态。"})
        snapshot = self.engine.snapshot(actor)
        return dump(snapshot["schedule_runtime"])

    @filter.llm_tool(name="brm_look")
    async def look(self, event: AstrMessageEvent, group_id: str = "") -> str:
        """查看当前位置和街区内的房间、出口；可用 group_id 查看指定街区。"""
        actor = await self._actor(event)
        if actor is None:
            return dump({"error": "无法确认真实 QQ 身份，未记录状态。"})
        if not isinstance(group_id, str):
            return dump({"error": "街区标识必须为字符串。"})
        if not group_id.strip():
            return dump(self.engine.look_indoor(actor))
        return dump(self.engine.look_indoor(actor, group_id.strip()))

    @filter.llm_tool(name="brm_move_to")
    async def move_to(self, event: AstrMessageEvent, room_id: str) -> str:
        """将角色移动到可经连接到达的房间或街区，并返回实际结果和当前感知。

        Args:
            room_id(string): 目标房间或街区标识，可从 brm_look 的结果中选择。
        """
        actor = await self._actor(event)
        if actor is None:
            return dump({"ok": False, "message": "无法确认真实 QQ 身份，未记录状态。"})
        try:
            result = self.engine.move_indoor(actor, room_id)
            move = result["move"]
            return dump(
                {
                    "ok": move["ok"],
                    "move": move,
                    "perception": self.engine.look_indoor(actor),
                }
            )
        except StateError as exc:
            return dump(
                {
                    "ok": False,
                    "message": str(exc),
                    "perception": self.engine.look_indoor(actor),
                }
            )

    @filter.llm_tool(name="brm_interact")
    async def perform_interaction(
        self, event: AstrMessageEvent, state_id: str, interaction_id: str
    ) -> str:
        """执行已配置的生活交互。仅在行为实际完成时调用；计划、假设、引用不算完成。程序检查条件并返回结果。

        Args:
            state_id(string): 该交互所属的状态标识。
            interaction_id(string): 查询或动态上下文中提供的交互标识。
        """
        actor = await self._actor(event)
        if actor is None:
            return dump({"ok": False, "message": "无法确认真实 QQ 身份，未记录状态。"})
        try:
            result = self.engine.execute(
                state_id,
                interaction_id,
                actor,
                token=self._token(event),
            )
            return dump(
                {
                    "ok": True,
                    "message": result["message"],
                    "states": self.engine.public(result["snapshot"]),
                }
            )
        except StateError as exc:
            return dump({"ok": False, "message": str(exc)})

    @filter.llm_tool(name="brm_report_event")
    async def report_event(
        self, event: AstrMessageEvent, state_id: str, event_id: str, evidence: str
    ) -> str:
        """报告本轮已经发生的已配置语义事件，由程序计算状态变化。同一事件每轮只结算一次。

        Args:
            state_id(string): 事件所属状态标识。
            event_id(string): 已配置的对话语义事件标识。
            evidence(string): 说明本轮具体哪段对话或行为表明事件已经发生。
        """
        actor = await self._actor(event)
        if actor is None:
            return dump({"ok": False, "message": "无法确认真实 QQ 身份，未记录状态。"})
        try:
            result = self.engine.execute(
                state_id,
                event_id,
                actor,
                source="semantic",
                evidence=evidence,
                token=self._token(event),
            )
            return dump(
                {
                    "ok": True,
                    "message": result["message"],
                    "states": self.engine.public(result["snapshot"]),
                }
            )
        except StateError as exc:
            return dump({"ok": False, "message": str(exc)})

    @filter.command_group("查询")
    def query_group(self):
        """查看当前真实QQ角色可见的状态、日程和地点。"""

    @query_group.command("状态")
    async def query_status(self, event: AstrMessageEvent):
        """查看当前真实QQ角色可见的状态和派生组。"""
        if not exact_query(self._query_message(event), "状态"):
            yield self._reply_query_usage(event)
            return
        actor = await self._actor(event)
        if actor is None:
            event.stop_event()
            yield event.plain_result("无法确认真实 QQ 身份，未创建虚拟会话或状态。")
            return
        snapshot = self.engine.snapshot(actor, include_paused_life=True)
        mode = self._query_mode(actor)
        event.stop_event()
        yield event.plain_result(render_states(snapshot, mode))

    @query_group.command("日程")
    async def query_schedule(self, event: AstrMessageEvent):
        """查看当前活动、固定日程和今日可安排的生活窗口。"""
        if not exact_query(self._query_message(event), "日程"):
            yield self._reply_query_usage(event)
            return
        actor = await self._actor(event)
        if actor is None:
            event.stop_event()
            yield event.plain_result("无法确认真实 QQ 身份，未创建虚拟会话或状态。")
            return
        snapshot = self.engine.snapshot(actor)
        mode = self._query_mode(actor)
        windows = None
        account_id = str(mode.get("account_id") or "")
        if mode.get("enabled") and account_id:
            getter = getattr(self.life, "available_action_windows", None)
            if callable(getter):
                windows = getter(account_id)
        event.stop_event()
        yield event.plain_result(
            render_schedule(
                snapshot,
                mode_name=str(mode.get("preset_name") or ""),
                activity=mode.get("activity"),
                windows=windows,
                mode_enabled=bool(mode.get("enabled")),
            )
        )

    @query_group.command("地点")
    async def query_location(self, event: AstrMessageEvent):
        """查看当前位置、完整分组路径和相邻地点。"""
        if not exact_query(self._query_message(event), "地点"):
            yield self._reply_query_usage(event)
            return
        actor = await self._actor(event)
        if actor is None:
            event.stop_event()
            yield event.plain_result("无法确认真实 QQ 身份，未创建虚拟会话或状态。")
            return
        snapshot = self.engine.snapshot(actor)
        event.stop_event()
        yield event.plain_result(render_location(snapshot))

    @filter.command("查询", priority=-sys.maxsize)
    async def query_unknown_command(
        self, event: AstrMessageEvent, subcommand: str = ""
    ):
        """拦截未知的查询子命令，防止其继续进入聊天处理。"""
        if self._has_active_query_subcommand(event):
            return
        event.stop_event()
        yield event.plain_result(query_usage(self._wake_prefix(event)))

    @filter.command("帮助", priority=sys.maxsize + 1)
    async def command_help(self, event: AstrMessageEvent):
        """显示 Better Real Me 的命令，以及当前会话匹配的其他帮助。"""
        own_help = help_text(self._wake_prefix(event))
        balance_active, balance_help = self._active_balance_help(event)
        if balance_active and balance_help is None:
            yield event.plain_result(own_help)
            return
        event.stop_event()
        sections = [own_help]
        if balance_help:
            sections.append(balance_help)
        yield event.plain_result("\n\n".join(sections))

    @filter.command("状态")
    async def status_command(self, event: AstrMessageEvent):
        actor = await self._actor(event)
        if actor is None:
            yield event.plain_result("无法确认真实 QQ 身份，未记录状态。")
            return
        snapshot = self.engine.snapshot(actor)
        rows = self.engine.public(snapshot)
        yield event.plain_result(
            "\n".join(f"{s['name']}：{s['value']} {s['stage']}" for s in rows)
            or "尚未创建状态，请在插件 View 的“状态设置”中新建。"
        )

    @filter.command("日程")
    async def schedule_command(self, event: AstrMessageEvent):
        actor = await self._actor(event)
        if actor is None:
            yield event.plain_result("无法确认真实 QQ 身份，未记录状态。")
            return
        runtime = self.engine.snapshot(actor)["schedule_runtime"]
        lines = [f"当前时间：{runtime['local_time']}"]
        active = runtime["active"]
        if active:
            description = (
                f"（{active['description']}）" if active["description"] else ""
            )
            lines.append(
                f"当前日程：{active['name']}{description}，至 {active['end_at']}"
            )
        else:
            lines.append("当前没有活动日程。")
        upcoming = runtime["next"]
        if upcoming:
            description = (
                f"（{upcoming['description']}）" if upcoming["description"] else ""
            )
            lines.append(
                f"下一日程：{upcoming['name']}{description}，{upcoming['start_at']} 至 {upcoming['end_at']}"
            )
        else:
            lines.append("没有已启用的后续日程。")
        yield event.plain_result("\n".join(lines))

    @filter.command("环境")
    async def indoor_command(self, event: AstrMessageEvent):
        actor = await self._actor(event)
        if actor is None:
            yield event.plain_result("无法确认真实 QQ 身份，未记录状态。")
            return
        runtime = self.engine.snapshot(actor)["indoor_runtime"]
        room = runtime["room"]
        if not room:
            yield event.plain_result("当前尚未配置环境房间。")
            return
        exits = runtime["exits"]
        lines = [f"当前位置：{room['name']}"]
        if room["description"]:
            lines.append(room["description"])
        lines.append("直接出口：" + ("、".join(item["name"] for item in exits) or "无"))
        yield event.plain_result("\n".join(lines))

    @filter.command("状态交互")
    async def interaction_command(
        self, event: AstrMessageEvent, state_id: str, interaction_id: str
    ):
        actor = await self._actor(event)
        if actor is None:
            yield event.plain_result("无法确认真实 QQ 身份，未记录状态。")
            return
        try:
            result = self.engine.execute(
                state_id,
                interaction_id,
                actor,
                source="user",
                token=self._token(event),
            )
            yield event.plain_result(result["message"])
        except StateError as exc:
            yield event.plain_result(str(exc))

    @filter.command("状态设置")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_command(self, event: AstrMessageEvent, state_id: str, value: str):
        import json

        actor = await self._actor(event)
        if actor is None:
            yield event.plain_result("无法确认真实 QQ 身份，未记录状态。")
            return
        try:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
            self.engine.adjust(state_id, actor, parsed)
            yield event.plain_result("状态已更新。")
        except StateError as exc:
            yield event.plain_result(str(exc))

    @filter.command("状态重置")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_command(self, event: AstrMessageEvent, state_id: str):
        actor = await self._actor(event)
        if actor is None:
            yield event.plain_result("无法确认真实 QQ 身份，未记录状态。")
            return
        try:
            self.engine.adjust(state_id, actor, reset=True)
            yield event.plain_result("状态已重置为初始值。")
        except StateError as exc:
            yield event.plain_result(str(exc))

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        try:
            await self.sharing.close()
        finally:
            try:
                await self.life_runner.close()
            finally:
                try:
                    await self.presence.close()
                finally:
                    self.engine.close()
