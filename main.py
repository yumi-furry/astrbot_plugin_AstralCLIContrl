# -*- coding: utf-8 -*-
"""Astral_CLI_contrl —— AstrBot 插件：远程管理 AstralGame-Cli 联机服务。

设计约束（严格遵循任务要求）：
    * 端点路径 / 请求方法 / 请求体字段 / 响应结构 / 鉴权方式 / 房间对象字段 /
      邀请码格式 / WebSocket 事件类型，一律以 AstralGame-Cli 官方 wiki 为准：
          - wiki/rest-api.md
          - wiki/rooms.md
          - wiki/websocket-events.md
    * wiki 未明确处一律以 ``# TODO:`` 标注并写明假设，不臆造字段名或端点。

REST 通道鉴权采用 wiki 推荐的 HTTP Basic Auth；WebSocket 通道走第三方服务端点
``/api/ws/service``（``ws_key`` 鉴权，不走面板账号），见 wiki/websocket-events.md：

    * 被动接收 ``snapshot`` / ``server.started`` / ``room.*`` / ``audit`` 事件并推送到指定会话；
    * 主动下发控制指令 ``get_room_info`` / ``open`` / ``restart`` / ``close`` / ``join``；
    * 控制类指令默认关闭（服务端 ``service_control_enabled``），失败时自动回退 REST 通道。

映射关系（严格对齐 wiki，未定义的映射不臆造）：
    * ``/agstart``    → WS ``open``（不带 game_id，重放上次配置）
    * ``/agrestart``  → WS ``restart``
    * ``/agcreate``   → WS ``open``（带 game_id / game_name / display_name）
    * ``/agjoin``     → WS ``join``（``input``）
    * ``/agleave``    → WS ``close``（停止实例并清空房间，与 REST leave 语义一致）
    * ``/agroom``     → WS ``get_room_info``（补齐成员列表与延迟）
    * ``/agstop``     → 仍走 REST：WS 协议无对应 action（``close`` 会额外清空房间，语义不同）
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

# AstrBot v4 起插件元数据改由 metadata.yaml 提供，@register 已从 astrbot.api.star 移除。
# 为兼容任务要求中的旧式写法与旧版 AstrBot，这里做导入降级处理。
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.api.star import register  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover

    def register(*_args: Any, **_kwargs: Any):
        """旧版元数据装饰器的空实现（元数据以 metadata.yaml 为准）。"""

        def _decorator(cls: Any) -> Any:
            return cls

        return _decorator


# ===========================================================================
# WebSocket 客户端加载：按文件路径动态加载 core/ws_client.py
#   避免以顶层包名 ``core`` 导入，防止与其他 AstrBot 插件的 core 模块重名。
# ===========================================================================

_WS_CLIENT_PATH = Path(__file__).resolve().parent / "core" / "ws_client.py"


def _load_ws_client() -> Any:
    """按文件路径加载同目录下的 ``core/ws_client.py`` 并返回模块对象。

    Raises:
        Exception: 文件缺失 / 语法错误 / 依赖缺失（如未安装 websockets）时抛出，
            由调用方捕获并降级为「仅 REST 通道」。
    """
    spec = importlib.util.spec_from_file_location(
        "astral_cli_contrl_ws_client", _WS_CLIENT_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失
        raise ImportError(f"未找到 WebSocket 客户端模块：{_WS_CLIENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# 邀请码规范化：严格对齐 wiki/rooms.md「三种邀请形式」与短码规范化规则
# ===========================================================================

# 短码规范化第 1 步：去除空白 / 连字符 / 下划线
_INVITE_SEP_RE = re.compile(r"[\s\-_]+")


def normalize_invite(text: str) -> str:
    """把用户输入规范化成可直接提交给 ``/api/room/join`` 的 ``input``。

    wiki/rooms.md 定义三种邀请形式：

    1. **短码**（如 ``ABC123``）：输入时先做规范化——去空白 / 连字符 / 下划线并转大写，
       ``I/L → 1``、``O → 0``（对齐 Crockford Base32 去歧义规则，允许看错字形）。
    2. **完整邀请链接**（如 ``https://next.astral.fan/j?c=ABC123``）：从 URL 的 ``c`` 参数
       提取令牌；令牌本身可能是短码，也可能是 ``AG1.`` 离线串，提取后按同一套规则再处理。
    3. **``AG1.`` 离线邀请串**（``AG1.`` + Base64urlNoPad(JSON)）：完全自包含、不依赖短码
       服务，原样返回（大小写敏感，不做去歧义替换）。

    Args:
        text: 用户原样输入的邀请码 / 完整链接 / 离线串。

    Returns:
        规范化后的字符串；输入为空或链接缺少 ``c`` 参数时返回空串。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    # 形式 2：完整邀请链接 → 提取 c 参数
    if raw.startswith("http://") or raw.startswith("https://"):
        token = parse_qs(urlsplit(raw).query).get("c", [""])[0].strip()
        if not token:
            return ""  # 链接未携带 c 参数，无法提取令牌
        raw = token

    # 形式 3：AG1. 离线串 → 原样返回
    if raw.startswith("AG1."):
        return raw

    # 形式 1：短码规范化
    code = _INVITE_SEP_RE.sub("", raw).upper()
    return code.replace("I", "1").replace("L", "1").replace("O", "0")


# ===========================================================================
# 默认文案（与 _conf_schema.json 中 messages 的 default 保持一致）
# ===========================================================================

DEFAULT_MESSAGES: dict[str, str] = {
    "msg_no_permission": "权限不足：该指令仅限 AstrBot 管理员使用。",
    "msg_error": "操作失败：{error}",
    "msg_unauthorized": "鉴权失败（401）：请检查插件配置中的用户名 / 密码。",
    "msg_room_empty": "当前未在房间中。",
    "msg_bad_invite": "邀请码为空或格式无效，请提供短码 / 完整邀请链接 / AG1. 离线串。",
    "msg_truncated": "\n…内容过长已截断（显示 {shown} 条，共 {total} 条）",
    "status": "AstralGame 节点状态\n运行中：{running}\n当前房间：{room_line}\n节点信息：{node_line}",
    "status_running_yes": "是",
    "status_running_no": "否",
    "status_unknown": "未知",
    "room_none_short": "无",
    "room_summary": "{game_name}（房间码 {short_code}）",
    "room": (
        "当前房间\n房主：{is_host}\n游戏：{game_name}（{game_id}）\n房间名：{display_name}\n"
        "房间码：{short_code}\n邀请链接：{share_url}\n离线邀请串：{offline_invite}\n中继节点：\n{peers}"
    ),
    "room_peers_none": "  （无）",
    "room_peer_item": "  {uri}",
    "logs": "AstralGame 运行日志（最近 {count} 行）\n{lines}",
    "logs_none": "AstralGame 运行日志：暂无日志。",
    "start_ok": "启动成功。{status_line}",
    "start_fail": "启动失败：{error}",
    "stop_ok": "已停止服务。",
    "stop_fail": "停止失败：{error}",
    "restart_ok": "重启成功。{status_line}",
    "restart_fail": "重启失败：{error}",
    "create_ok": (
        "房间创建成功\n游戏：{game_name}（{game_id}）\n房间码：{short_code}\n"
        "分享链接：{share_url}\n离线邀请串：{offline_invite}"
    ),
    "create_fail": "房间创建失败：{error}",
    "invite_ok": "当前房间分享链接：{share_url}\n房间码：{short_code}",
    "join_ok": (
        "已加入房间\n游戏：{game_name}（{game_id}）\n房间码：{short_code}\n分享链接：{share_url}"
    ),
    "join_fail": "加入房间失败：{error}",
    "leave_ok": "已离开房间，房间已关闭。",
    "leave_fail": "离开房间失败：{error}",
    "servers": "中继服务器（共 {count} 个）\n{items}",
    "servers_none": "暂无已配置的中继服务器。",
    "server_item": "{index}. {name}｜{uri}｜{enabled}",
    "server_enabled_yes": "已启用",
    "server_enabled_no": "已禁用",
    "ping": "中继服务器测速（单位 ms，-1 表示失败 / 超时）\n{items}",
    "ping_item": "{index}. {name}｜{uri}｜{latency}",
    "audit": "操作记录（最近 {count} 条）\n{items}",
    "audit_none": "暂无操作记录。",
    "audit_item": "{time}｜{kind}｜{action}｜{ip}",
    # ---- WebSocket 通道相关文案（与 _conf_schema.json 保持一致）----
    "channel_ws": "\n（经 WebSocket 通道下发）",
    "channel_rest": "",
    "ws_disabled": "WebSocket 通道未启用：请在插件配置中开启 ws_enabled 并填写 ws_key。",
    "ws_not_connected": "WebSocket 通道未连接（正在重连，或密钥已失效）。",
    "ws_status": (
        "WebSocket 通道状态\n地址：{url}\n启用：{enabled}\n连接：{connected}\n"
        "服务名称：{name}（{type}）\n最近事件：{last_event}\n推送事件：{push_events}"
    ),
    "ws_room_info": "\n节点成员（共 {count} 个）：\n{members}",
    "ws_room_member_item": "  {hostname}｜{ipv4}｜{latency} ms｜丢包 {loss}%｜{conn_type}",
    "ws_room_members_none": "  （无成员）",
    "ws_push": "[AstralGame] {event}\n{detail}",
    "ws_push_audit": "[AstralGame] 操作记录\n{time}｜{kind}｜{action}｜{ip}",
}

# 服务端接口的硬性上限（见 wiki/rest-api.md 的 /api/logs 与 /api/audit-logs）
_LOGS_MAX = 800
_AUDIT_MAX = 500


class _SafeDict(dict):
    """格式化占位符时缺失的键保持原样（``{key}``），避免用户自定义文案导致 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return "{" + key + "}"


class AstralError(Exception):
    """插件内部统一错误：携带面向用户的友好中文信息与可选 HTTP 状态码。"""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        self.status = status
        super().__init__(message)


@register("Astral_CLI_contrl", "AstralGame", "远程管理 AstralGame-Cli 联机服务", "1.1.0")
class AstralCliControl(Star):
    """AstralGame-Cli 远程管理插件。

    AstrBot 约定：
        * 插件类继承 ``Star``；
        * ``__init__(self, context, config)`` 中初始化 HTTP 客户端与配置；
        * 指令处理函数签名为 ``async def xxx(self, event: AstrMessageEvent)``，
          通过 ``yield event.plain_result(...)`` 返回消息；
        * 卸载 / 重载时调用 ``terminate()`` 关闭 HTTP 会话。
    """

    # -------------------------------------------------------------- 生命周期

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None) -> None:
        super().__init__(context)
        # config 为 AstrBotConfig（继承自 dict）；未提供时退化为空字典以保证健壮性
        self.config: Any = config if config is not None else {}

        self.base_url: str = str(self._cfg("base_url", "http://127.0.0.1:8080")).rstrip("/")
        self.username: str = str(self._cfg("username", "") or "")
        self.password: str = str(self._cfg("password", "") or "")
        self.timeout: int = max(1, int(self._cfg("timeout", 10)))
        self.log_tail_default: int = int(self._cfg("log_tail_default", 100))
        self.audit_tail_default: int = int(self._cfg("audit_tail_default", 20))
        self.max_output_chars: int = max(200, int(self._cfg("max_output_chars", 3000)))

        self._session: Optional[aiohttp.ClientSession] = None

        # ---- WebSocket 通道配置（见 wiki/websocket-events.md）----
        self.ws_enabled: bool = bool(self._cfg("ws_enabled", False))
        self.ws_key: str = str(self._cfg("ws_key", "") or "")
        self.ws_name: str = str(self._cfg("ws_name", "AstrBot") or "AstrBot")
        self.ws_type: str = str(self._cfg("ws_type", "AstrBot") or "AstrBot")
        self.ws_url_override: str = str(self._cfg("ws_url", "") or "")
        self.ws_push_targets: list[str] = self._split_targets(
            self._cfg("ws_push_targets", "")
        )
        self.ws_push_events: set[str] = self._split_events(
            self._cfg(
                "ws_push_events",
                "room.created,room.joined,room.left,server.started,audit",
            )
        )
        self._ws: Any = None  # AstralWsClient 实例（延迟创建）
        self._ws_started: bool = False
        self._ws_last_event: str = ""
        self._ws_last_ts: float = 0.0

        logger.info(
            "[Astral_CLI_contrl] 插件已加载，目标服务：%s（鉴权：%s，WebSocket：%s）",
            self.base_url,
            "启用" if self.username else "关闭",
            "启用" if (self.ws_enabled and self.ws_key) else "关闭",
        )
        # 启动 WS 连接。__init__ 为同步方法，可能不在事件循环中；
        # 若当前无运行中的循环则跳过，改由首个指令触发惰性连接。
        if self.ws_enabled and self.ws_key:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                logger.info(
                    "[Astral_CLI_contrl] 当前无运行中的事件循环，WebSocket 将在首个指令时惰性连接。"
                )
            else:
                self._ensure_ws_started()

    async def terminate(self) -> None:
        """插件被卸载 / 停用时关闭 HTTP 会话与 WebSocket 连接。"""
        if self._ws is not None:
            try:
                await self._ws.stop()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不影响卸载
                logger.warning("[Astral_CLI_contrl] 关闭 WebSocket 连接失败：%s", exc)
            self._ws = None
        self._ws_started = False
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("[Astral_CLI_contrl] 插件已卸载，HTTP 会话与 WebSocket 连接已关闭。")

    # -------------------------------------------------------------- 配置 / 文案

    def _cfg(self, key: str, default: Any) -> Any:
        """安全读取插件配置（缺失 / 为 None 时回退默认值）。"""
        value = self.config.get(key, default) if self.config else default
        return default if value is None else value

    def _tpl(self, key: str, **fields: Any) -> str:
        """取可配置文案并填充占位符；缺失占位符原样保留。"""
        messages = self._cfg("messages", {}) or {}
        template = messages.get(key) or DEFAULT_MESSAGES.get(key, "")
        return str(template).format_map(_SafeDict(fields))

    # -------------------------------------------------------------- HTTP 层

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            auth = (
                aiohttp.BasicAuth(self.username, self.password) if self.username else None
            )
            self._session = aiohttp.ClientSession(
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "astrbot-plugin-astral-cli-contrl"},
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """统一请求封装：拼接 base_url、注入 Basic Auth、处理超时与异常、解析 JSON。

        鉴权方式为 wiki/rest-api.md 推荐的 HTTP Basic Auth
        （``Authorization: Basic base64(username:password)``；username 为空时服务端关闭鉴权）。

        Args:
            method: HTTP 方法（GET / POST / PUT / DELETE）。
            path: 以 ``/api`` 开头的接口路径。
            json_body: JSON 请求体（POST / PUT 使用）。
            params: URL 查询参数。

        Returns:
            解析后的响应体；非 JSON 响应返回原始文本。

        Raises:
            AstralError: 网络异常 / 超时 / 401 / 4xx / 5xx，均附带面向用户的中文提示。
        """
        session = await self._get_session()
        url = self.base_url + path
        try:
            async with session.request(
                method, url, json=json_body, params=params
            ) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise AstralError(self._tpl("msg_unauthorized"), 401)
                if resp.status >= 400:
                    # 错误响应统一为 {"error": "..."}（见 wiki/rest-api.md）
                    raise AstralError(self._extract_error(text), resp.status)
                return self._parse_json(text)
        except AstralError:
            raise
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            raise AstralError("请求超时：AstralGame-Cli 服务未在规定时间内响应，请稍后重试。") from None
        except aiohttp.ClientError as exc:
            raise AstralError(
                f"无法连接到 AstralGame-Cli 服务，请检查地址与网络（{exc.__class__.__name__}）。"
            ) from None

    @staticmethod
    def _extract_error(text: str) -> str:
        """从错误响应中提取可读信息（优先 ``error`` 字段）。"""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return text.strip() or "服务返回未知错误"
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
        return str(data)

    @staticmethod
    def _parse_json(text: str) -> Any:
        """解析 JSON；失败时返回原始文本（部分接口可能返回纯文本）。"""
        if not text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text

    # ============================================================== WebSocket 层
    # 端点与协议严格对齐 wiki/websocket-events.md：
    #   ws://<host>:<port>/api/ws/service?key=<ws_key>&name=<名称>&type=<类型>
    # 连接后先收 hello、再收 snapshot，之后实时推送事件；控制指令走 {"id","action"} 帧。

    @staticmethod
    def _split_targets(raw: Any) -> list[str]:
        """解析推送目标会话串（逗号 / 分号 / 换行分隔）。"""
        if not raw:
            return []
        parts = re.split(r"[,;\n]+", str(raw))
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _split_events(raw: Any) -> set[str]:
        """解析需要推送的事件类型（逗号分隔）。"""
        if not raw:
            return set()
        return {p.strip() for p in re.split(r"[,;\s]+", str(raw)) if p.strip()}

    def _ws_resolve_url(self) -> str:
        """确定 WS 服务地址：优先 ``ws_url``，否则由 ``base_url`` 推导。"""
        if self.ws_url_override:
            return self.ws_url_override
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base.rstrip("/") + "/api/ws/service"

    def _ensure_ws_started(self) -> None:
        """惰性创建并启动 WS 客户端（幂等；需存在运行中的事件循环）。"""
        if self._ws_started or not self.ws_enabled or not self.ws_key:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环，留待下次
        self._ws_started = True
        try:
            module = _load_ws_client()
            self._ws = module.AstralWsClient(
                url=self._ws_resolve_url(),
                key=self.ws_key,
                name=self.ws_name,
                service_type=self.ws_type,
                request_timeout=self.timeout,
                on_event=self._on_ws_event,
            )
            self._ws.start()
            logger.info(
                "[Astral_CLI_contrl] WebSocket 通道已启动：%s（name=%s, type=%s）",
                self._ws_resolve_url(),
                self.ws_name,
                self.ws_type,
            )
        except Exception as exc:  # noqa: BLE001 - 未安装 websockets / 文件缺失时降级为仅 REST
            self._ws = None
            self._ws_started = False
            logger.warning(
                "[Astral_CLI_contrl] WebSocket 客户端初始化失败，将仅使用 REST 通道：%s", exc
            )

    def _ws_ready(self) -> bool:
        """WS 是否已连接可用。"""
        return self._ws is not None and bool(getattr(self._ws, "connected", False))

    async def _ws_control(self, action: str, **fields: Any) -> tuple[bool, bool, Any]:
        """尝试经 WS 下发控制指令。

        Returns:
            ``(via_ws, ok, payload)``：
                * ``via_ws=False``：WS 未连接，调用方应回退 REST；
                * ``via_ws=True, ok=True``：payload 为应答 ``data``；
                * ``via_ws=True, ok=False``：payload 为服务端错误文本（如未开启
                  ``service_control_enabled``），调用方通常仍应回退 REST。
        """
        self._ensure_ws_started()
        if not self._ws_ready():
            return False, False, ""
        try:
            data = await self._ws.request(action, **fields)
            return True, True, data
        except Exception as exc:  # noqa: BLE001 - 统一回退 REST
            logger.info(
                "[Astral_CLI_contrl] WS 指令 %s 失败，将回退 REST：%s", action, exc
            )
            return True, False, str(exc)

    def _ws_channel(self, via_ws: bool) -> str:
        """返回通道标记文案（经 WS 下发时附加提示，经 REST 时默认为空）。"""
        return self._tpl("channel_ws") if via_ws else self._tpl("channel_rest")

    async def _ws_members_text(self) -> str:
        """经 WS ``get_room_info`` 获取成员列表（含实测延迟），失败返回空串。"""
        self._ensure_ws_started()
        if not self._ws_ready():
            return ""
        try:
            data = await self._ws.request("get_room_info")
        except Exception as exc:  # noqa: BLE001
            logger.info("[Astral_CLI_contrl] WS get_room_info 失败：%s", exc)
            return ""
        if not isinstance(data, dict):
            return ""
        members = data.get("members")
        if not isinstance(members, list) or not members:
            return self._tpl(
                "ws_room_info", count=0, members=self._tpl("ws_room_members_none")
            )
        lines: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            lines.append(
                self._tpl(
                    "ws_room_member_item",
                    hostname=member.get("hostname") or member.get("peer_id") or "-",
                    ipv4=member.get("ipv4") or "-",
                    latency=(
                        member["latency_ms"] if member.get("latency_ms") is not None else "-"
                    ),
                    loss=(
                        member["loss_rate"] if member.get("loss_rate") is not None else "-"
                    ),
                    conn_type=member.get("conn_type") or "-",
                )
            )
        return self._tpl("ws_room_info", count=len(lines), members="\n".join(lines))

    async def _on_ws_event(self, msg: dict) -> None:
        """WS 事件回调：记录最近事件，并按配置推送到目标会话。"""
        event = str(msg.get("event") or "")
        if not event:
            return
        self._ws_last_event = event
        self._ws_last_ts = time.time()

        if event == "hello":
            logger.info("[Astral_CLI_contrl] WS 握手完成：%s", msg)
            return
        if event == "result":
            return  # 指令应答由客户端按 id 关联，不在此处理
        if not self.ws_push_targets:
            return
        if self.ws_push_events and event not in self.ws_push_events:
            return

        text = self._render_ws_event(event, msg)
        if not text:
            return
        for umo in self.ws_push_targets:
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001 - 单个目标失败不影响其他
                logger.warning("[Astral_CLI_contrl] 事件推送到 %s 失败：%s", umo, exc)

    def _render_ws_event(self, event: str, msg: dict) -> str:
        """把 WS 事件消息渲染成推送文本（字段见 wiki 消息格式）。"""
        if event == "audit":
            entry = msg.get("entry") if isinstance(msg.get("entry"), dict) else {}
            return self._tpl(
                "ws_push_audit",
                time=self._fmt_ts(entry.get("ts") or msg.get("timestamp")),
                kind=entry.get("kind") or "-",
                action=entry.get("action") or "-",
                ip=entry.get("ip") or "-",
            )
        room = msg.get("room") if isinstance(msg.get("room"), dict) else {}
        if not room:
            detail = self._tpl("room_none_short")
        else:
            detail = self._tpl(
                "room_summary",
                game_name=room.get("display_name") or room.get("game_name") or "未知",
                short_code=room.get("short_code") or self._tpl("room_none_short"),
            )
            if msg.get("share_url"):
                detail += "\n" + str(msg["share_url"])
        return self._tpl("ws_push", event=event, detail=detail)

    async def _do_ws_status(self) -> str:
        """查看 WebSocket 通道状态（``/agws``）。"""
        if not self.ws_enabled or not self.ws_key:
            return self._tpl("ws_disabled")
        self._ensure_ws_started()
        connected = self._ws_ready()
        last_event = (
            f"{self._ws_last_event}（{self._fmt_ts(self._ws_last_ts)}）"
            if self._ws_last_event
            else "-"
        )
        return self._tpl(
            "ws_status",
            url=self._ws_resolve_url(),
            enabled=self._tpl("status_running_yes"),
            connected=(
                self._tpl("status_running_yes") if connected else self._tpl("status_running_no")
            ),
            name=self.ws_name,
            type=self.ws_type,
            last_event=last_event,
            push_events="，".join(sorted(self.ws_push_events)) or "-",
        )

    # -------------------------------------------------------------- 渲染工具

    def _err_text(self, exc: Exception) -> str:
        """把异常转成面向用户的中文回复（不暴露异常栈）。"""
        if isinstance(exc, AstralError):
            return str(exc) if exc.status == 401 else self._tpl("msg_error", error=str(exc))
        logger.error("[Astral_CLI_contrl] 未预期的异常：%s", exc, exc_info=True)
        return self._tpl("msg_error", error="内部错误，请查看 AstrBot 日志")

    def _clip(self, text: str, total: Optional[int] = None, shown: Optional[int] = None) -> str:
        """按 ``max_output_chars`` 截断长文本，并可选追加截断提示。"""
        if len(text) <= self.max_output_chars:
            return text
        clipped = text[: self.max_output_chars]
        if total is not None and shown is not None:
            clipped += self._tpl("msg_truncated", shown=shown, total=total)
        else:
            clipped += "…"
        return clipped

    @staticmethod
    def _yes_no(flag: Any) -> str:
        return "是" if flag else "否"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """管理员校验。

        使用的 AstrBot 权限判断 API：``AstrMessageEvent.is_admin()``
        （其实现等价于 ``event.role == "admin"``）。
        亦可在过滤器层使用装饰器 ``@filter.permission_type(filter.PermissionType.ADMIN)``，
        但那样无法自定义「权限不足」文案，因此这里在运行期判断。
        """
        try:
            return bool(event.is_admin())
        except Exception:  # pragma: no cover - 兼容极旧版本无该方法
            return getattr(event, "role", "") == "admin"

    # -------------------------------------------------------------- 数据归一化

    def _split_room(self, data: Any) -> tuple[dict, str]:
        """从 ``/api/room/current`` 等响应中取出 (room, share_url)。

        wiki 明确：``/api/room/current`` 返回当前房间与分享链接，无房间时 ``room`` 为 ``null``。
        对创建 / 加入接口 wiki 未逐一给出响应结构，这里做防御性归一化。
        """
        if not isinstance(data, dict):
            return {}, ""
        room = data.get("room")
        if room is None and data.get("network_name"):
            # TODO: wiki 未明确 /api/room/create | join 的响应结构，
            #  假设既可能是 {"room": {...}, "share_url": ...}，也可能直接返回房间对象。
            room = data
        share = data.get("share_url") or ""
        return (room if isinstance(room, dict) else {}), str(share)

    def _render_room(self, room: dict, share_url: str) -> str:
        """渲染房间详情（字段取自 wiki/rooms.md 的「房间数据模型」）。"""
        peers: Iterable[Any] = room.get("peers") or []
        peer_lines = [
            self._tpl("room_peer_item", uri=(p.get("uri") if isinstance(p, dict) else p) or "-")
            for p in peers
        ]
        return self._tpl(
            "room",
            is_host=self._yes_no(room.get("is_host")),
            game_name=room.get("game_name") or "未知",
            game_id=room.get("game_id") or "-",
            display_name=room.get("display_name") or room.get("game_name") or "-",
            short_code=room.get("short_code") or self._tpl("room_none_short"),
            share_url=share_url or "-",
            offline_invite=room.get("offline_invite") or "-",
            peers="\n".join(peer_lines) if peer_lines else self._tpl("room_peers_none"),
        )

    def _status_summary(self, status: Any, room: dict, share_url: str) -> tuple[str, str, str]:
        """从 ``/api/status`` 快照中归纳出（运行中, 房间摘要, 节点摘要）。

        TODO: wiki/rest-api.md 仅说明 ``/api/status`` 是「节点状态快照」，
          未列出字段名；此处按事件消息中 ``running`` / ``node.*`` 的命名做兼容取值，
          待文档补充后收敛。
        """
        data = status if isinstance(status, dict) else {}
        node = data.get("node") if isinstance(data.get("node"), dict) else data

        running_flag = data.get("running")
        if running_flag is None:
            running_flag = data.get("is_running")
        if running_flag is None and isinstance(node, dict):
            running_flag = node.get("running")
        running = self._tpl("status_running_yes") if running_flag is True else (
            self._tpl("status_running_no") if running_flag is False else self._tpl("status_unknown")
        )

        if room:
            room_line = self._tpl(
                "room_summary",
                game_name=room.get("display_name") or room.get("game_name") or "未知",
                short_code=room.get("short_code") or self._tpl("room_none_short"),
            )
        else:
            room_line = self._tpl("room_none_short")

        node_bits = []
        if isinstance(node, dict):
            if node.get("my_ipv4"):
                node_bits.append(f"虚拟 IP {node['my_ipv4']}")
            if node.get("total_nodes") is not None:
                node_bits.append(f"在线节点 {node['total_nodes']}")
            if node.get("server_version"):
                node_bits.append(f"服务端 v{node['server_version']}")
            if node.get("easytier_version"):
                node_bits.append(f"EasyTier {node['easytier_version']}")
            if node.get("error"):
                node_bits.append(f"错误：{node['error']}")
        node_line = "，".join(node_bits) if node_bits else "-"
        return running, room_line, node_line

    @staticmethod
    def _fmt_ts(unix: Any) -> str:
        """Unix 秒 → 本地时间字符串。"""
        try:
            return time.strftime("%m-%d %H:%M:%S", time.localtime(float(unix)))
        except (TypeError, ValueError, OSError):
            return str(unix)

    @staticmethod
    def _norm_log_lines(data: Any) -> list[str]:
        """归一化 ``/api/logs`` 响应为日志行列表。

        TODO: wiki 只说返回「节点环形日志，默认 200 条、上限 800 条」，
          未给出响应字段名；兼容纯数组 / {"logs"|"lines"|"items": [...]} / 纯文本三种形态。
        """
        if isinstance(data, list):
            return [str(x) for x in data]
        if isinstance(data, dict):
            for key in ("logs", "lines", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [str(x) for x in value]
            return [json.dumps(data, ensure_ascii=False)]
        if isinstance(data, str):
            return data.splitlines()
        return []

    @staticmethod
    def _norm_servers(data: Any) -> list[dict]:
        """归一化 ``/api/servers`` 列表。

        TODO: wiki 只说明「全部中继服务器（含启用状态、排序）」；
          据 POST/PUT 请求体（name / uri / enabled）与路径中的 id，
          假设条目字段为 {id, name, uri, enabled}，做兼容取值。
        """
        items = data
        if isinstance(data, dict):
            items = data.get("servers") or data.get("items") or []
        result: list[dict] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                result.append(
                    {
                        "id": item.get("id"),
                        "name": str(item.get("name") or item.get("uri") or "?"),
                        "uri": str(item.get("uri") or "-"),
                        "enabled": bool(item.get("enabled", True)),
                    }
                )
        return result

    @staticmethod
    def _norm_ping(data: Any) -> list[dict]:
        """归一化 ``/api/servers/ping`` 结果。

        wiki：并发测速所有服务器，返回 TCP 连接耗时（毫秒；失败 / 超时返回 -1）。
        TODO: 未给出响应结构，兼容 list[dict] 与 dict{uri: ms} 两种形态，
          耗时字段兼容 latency / latency_ms / ms / elapsed / time。
        """
        rows: list[dict] = []

        def _pick_latency(obj: dict) -> Any:
            for key in ("latency", "latency_ms", "ms", "elapsed", "time", "tcp_ms"):
                if key in obj:
                    return obj[key]
            return "-"

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    rows.append(
                        {
                            "name": str(item.get("name") or item.get("uri") or "?"),
                            "uri": str(item.get("uri") or "-"),
                            "latency": _pick_latency(item),
                        }
                    )
        elif isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    rows.append(
                        {
                            "name": str(value.get("name") or key),
                            "uri": str(value.get("uri") or key),
                            "latency": _pick_latency(value),
                        }
                    )
                else:
                    rows.append({"name": key, "uri": key, "latency": value})
        return rows

    # ======================================================================
    # 业务逻辑（与指令注册解耦，供「/astral <子指令>」与「/ag<子指令>」两种入口复用）
    # ======================================================================

    def _guard_admin(self, event: AstrMessageEvent) -> Optional[str]:
        """管理员校验：通过返回 ``None``；不通过返回「权限不足」文案。"""
        if self._is_admin(event):
            return None
        return self._tpl("msg_no_permission")

    async def _do_status(self) -> str:
        """查询服务运行状态：``/api/status`` + ``/api/room/current``。"""
        self._ensure_ws_started()  # 顺带确保 WS 通道已启动（事件推送依赖它）
        try:
            status = await self._request("GET", "/api/status")
            current = await self._request("GET", "/api/room/current")
        except Exception as exc:  # noqa: BLE001 - 统一转为用户可读文案
            return self._err_text(exc)
        room, share_url = self._split_room(current)
        running, room_line, node_line = self._status_summary(status, room, share_url)
        return self._tpl("status", running=running, room_line=room_line, node_line=node_line)

    async def _do_room(self) -> str:
        """查询当前房间（房主 / 游戏 / 短码 / 分享链接 / 中继节点）。

        若 WebSocket 通道已连接，额外经 ``get_room_info`` 补齐成员列表（含实测延迟 /
        丢包率 / 连接类型，见 wiki/websocket-events.md）；未连接则仅输出 REST 信息。
        """
        try:
            current = await self._request("GET", "/api/room/current")
        except Exception as exc:  # noqa: BLE001
            return self._err_text(exc)
        room, share_url = self._split_room(current)
        if not room:
            return self._tpl("msg_room_empty")
        text = self._render_room(room, share_url)
        members_text = await self._ws_members_text()
        return text + members_text if members_text else text

    async def _do_logs(self, event: AstrMessageEvent, tail: int) -> str:
        """查询运行日志（默认 ``log_tail_default`` 行，服务端上限 800 行）。

        需管理员权限（对应任务清单中 ``/aglogs`` 的「管理员权限需求」）。
        """
        denied = self._guard_admin(event)
        if denied:
            return denied
        rows = tail if tail and tail > 0 else self.log_tail_default
        rows = max(1, min(int(rows), _LOGS_MAX))
        try:
            data = await self._request("GET", "/api/logs", params={"tail": rows})
        except Exception as exc:  # noqa: BLE001
            return self._err_text(exc)
        lines = self._norm_log_lines(data)
        if not lines:
            return self._tpl("logs_none")
        shown = len(lines)
        body = "\n".join(lines)
        # 太长时按字符截断，并从最新一行往前保留，提示已显示条数与总条数
        if len(body) > self.max_output_chars:
            kept: list[str] = []
            size = 0
            for line in reversed(lines):
                size += len(line) + 1
                if size > self.max_output_chars:
                    break
                kept.append(line)
            kept.reverse()
            body = "\n".join(kept)
            body += self._tpl("msg_truncated", shown=len(kept), total=shown)
        return self._tpl("logs", count=shown, lines=body)

    async def _do_start(self, event: AstrMessageEvent) -> str:
        """启动（或重启）EasyTier 实例。需管理员权限。

        优先经 WS ``open``（不带 game_id，重放上次配置）；未连接或失败则回退 REST ``/api/start``。
        """
        denied = self._guard_admin(event)
        if denied:
            return denied
        via_ws, ok, _payload = await self._ws_control("open")
        if via_ws and ok:
            status_line = await self._brief_status()
            return self._tpl("start_ok", status_line=status_line) + self._ws_channel(True)
        try:
            # 不携带 TOML 文本：由服务端使用自身配置启动（见 wiki /api/start）
            await self._request("POST", "/api/start")
            status_line = await self._brief_status()
        except Exception as exc:  # noqa: BLE001
            return self._tpl("start_fail", error=self._err_text(exc))
        return self._tpl("start_ok", status_line=status_line) + self._ws_channel(False)

    async def _do_stop(self, event: AstrMessageEvent) -> str:
        """停止当前实例。需管理员权限。

        仅走 REST ``/api/stop``：WS 第三方服务协议未定义 ``stop`` action
        （其 ``close`` 会额外清空当前房间，语义不同，故不映射以免行为不一致）。
        """
        denied = self._guard_admin(event)
        if denied:
            return denied
        try:
            await self._request("POST", "/api/stop")
        except Exception as exc:  # noqa: BLE001
            return self._tpl("stop_fail", error=self._err_text(exc))
        return self._tpl("stop_ok")

    async def _do_restart(self, event: AstrMessageEvent) -> str:
        """用最近一次配置重启实例。需管理员权限。

        优先经 WS ``restart``；未连接或失败则回退 REST ``/api/restart``。
        """
        denied = self._guard_admin(event)
        if denied:
            return denied
        via_ws, ok, _payload = await self._ws_control("restart")
        if via_ws and ok:
            status_line = await self._brief_status()
            return self._tpl("restart_ok", status_line=status_line) + self._ws_channel(True)
        try:
            await self._request("POST", "/api/restart")
            status_line = await self._brief_status()
        except Exception as exc:  # noqa: BLE001
            return self._tpl("restart_fail", error=self._err_text(exc))
        return self._tpl("restart_ok", status_line=status_line) + self._ws_channel(False)

    async def _brief_status(self) -> str:
        """操作后返回一条简要状态（失败时静默降级，不影响主流程）。"""
        try:
            current = await self._request("GET", "/api/room/current")
        except Exception:  # noqa: BLE001
            return ""
        room, _ = self._split_room(current)
        if room:
            return self._tpl(
                "room_summary",
                game_name=room.get("display_name") or room.get("game_name") or "未知",
                short_code=room.get("short_code") or self._tpl("room_none_short"),
            )
        return self._tpl("room_none_short")

    async def _do_create(self, game_id: str, name: str) -> str:
        """创建房间：``game_id`` 取内置游戏目录（``/api/games``，``custom`` 表示自定义网络）。

        ``name``（可选）作为房间展示名 ``display_name``；当 ``game_id`` 不在目录中时，
        用 ``name`` / ``game_id`` 兜底 ``game_name``（``/api/room/create`` 的请求体必填
        ``game_name``，见 wiki/rooms.md）。
        """
        game_id = (game_id or "").strip()
        if not game_id:
            return self._tpl(
                "msg_error", error="请提供游戏 ID（可从 /api/games 获取，custom 表示自定义网络）"
            )

        game_name = ""
        try:
            catalog = await self._request("GET", "/api/games")
            game_name = self._lookup_game_name(catalog, game_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Astral_CLI_contrl] 读取游戏目录失败：%s", exc)

        # TODO: wiki 未明确 /api/games 条目的字段名（仅在 _lookup_game_name 中按
        #  game_id/id 与 game_name/name 兼容）；未知游戏时用名称 / ID 兜底。
        if not game_name:
            game_name = (name or "").strip() or game_id

        payload: dict[str, Any] = {"game_id": game_id, "game_name": game_name}
        if (name or "").strip():
            payload["display_name"] = name.strip()

        # 优先经 WS ``open``（携带 game_id / game_name 走建房流程，见 wiki 指令协议）
        via_ws, ok, data = await self._ws_control("open", **payload)
        if not (via_ws and ok):
            try:
                data = await self._request("POST", "/api/room/create", json_body=payload)
            except Exception as exc:  # noqa: BLE001
                return self._tpl("create_fail", error=self._err_text(exc))

        room, share_url = self._split_room(data)
        if not share_url:
            share_url = await self._share_url_from_current()
        if not room.get("game_name"):
            room = {
                **room,
                "game_id": room.get("game_id") or game_id,
                "game_name": room.get("game_name") or game_name,
            }
        return self._tpl(
            "create_ok",
            game_name=room.get("game_name") or game_name,
            game_id=room.get("game_id") or game_id,
            # 短码不可用时回退离线串（见 wiki/rooms.md「分享链接」）
            short_code=room.get("short_code") or room.get("offline_invite") or "-",
            share_url=share_url or "-",
            offline_invite=room.get("offline_invite") or "-",
        ) + self._ws_channel(via_ws and ok)

    @staticmethod
    def _lookup_game_name(catalog: Any, game_id: str) -> str:
        """在 ``/api/games`` 内置游戏目录中按 ID 查名称。

        TODO: wiki 只注明「内置游戏目录（硬编码）」，未给出条目字段名；
          兼容 game_id/id/gameId 与 game_name/name 两种命名。
        """
        items = catalog
        if isinstance(catalog, dict):
            items = catalog.get("games") or catalog.get("items") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                gid = item.get("game_id") or item.get("id") or item.get("gameId")
                if gid and str(gid) == game_id:
                    return str(item.get("game_name") or item.get("name") or gid)
        return ""

    async def _share_url_from_current(self) -> str:
        """兜底：从 ``/api/room/current`` 补齐分享链接。"""
        try:
            current = await self._request("GET", "/api/room/current")
        except Exception:  # noqa: BLE001
            return ""
        _, share_url = self._split_room(current)
        return share_url

    async def _do_invite(self) -> str:
        """读取当前房间的分享链接并回复；无房间时提示。"""
        try:
            current = await self._request("GET", "/api/room/current")
        except Exception as exc:  # noqa: BLE001
            return self._err_text(exc)
        room, share_url = self._split_room(current)
        if not room or not share_url:
            return self._tpl("msg_room_empty")
        return self._tpl(
            "invite_ok",
            share_url=share_url,
            short_code=room.get("short_code") or room.get("offline_invite") or "-",
        )

    async def _do_join(self, invite: str) -> str:
        """加入房间：自动识别短码 / 完整链接 / AG1. 离线串并规范化后提交。

        优先经 WS ``join``（``input``）；未连接或失败则回退 REST ``/api/room/join``。
        """
        normalized = normalize_invite(invite or "")
        if not normalized:
            return self._tpl("msg_bad_invite")
        via_ws, ok, data = await self._ws_control("join", input=normalized)
        if not (via_ws and ok):
            try:
                # 请求体字段 input：短码 / 完整链接 / AG1. 离线串三选一（见 wiki/rooms.md）
                data = await self._request(
                    "POST", "/api/room/join", json_body={"input": normalized}
                )
            except Exception as exc:  # noqa: BLE001
                return self._tpl("join_fail", error=self._err_text(exc))
        room, share_url = self._split_room(data)
        if not share_url:
            share_url = await self._share_url_from_current()
        return self._tpl(
            "join_ok",
            game_name=room.get("game_name") or "未知",
            game_id=room.get("game_id") or "-",
            short_code=room.get("short_code") or room.get("offline_invite") or "-",
            share_url=share_url or "-",
        ) + self._ws_channel(via_ws and ok)

    async def _do_leave(self) -> str:
        """离开房间：停止实例、清空当前房间。

        优先经 WS ``close``（语义与 REST ``/api/room/leave`` 一致，均会停实例并清空房间）；
        未连接或失败则回退 REST。
        """
        via_ws, ok, _payload = await self._ws_control("close")
        if via_ws and ok:
            return self._tpl("leave_ok") + self._ws_channel(True)
        try:
            await self._request("POST", "/api/room/leave")
        except Exception as exc:  # noqa: BLE001
            return self._tpl("leave_fail", error=self._err_text(exc))
        return self._tpl("leave_ok") + self._ws_channel(False)

    async def _do_servers(self) -> str:
        """列出已配置的中继服务器（含启用状态）。"""
        try:
            data = await self._request("GET", "/api/servers")
        except Exception as exc:  # noqa: BLE001
            return self._err_text(exc)
        servers = self._norm_servers(data)
        if not servers:
            return self._tpl("servers_none")
        items = "\n".join(
            self._tpl(
                "server_item",
                index=i,
                name=s["name"],
                uri=s["uri"],
                enabled=self._tpl(
                    "server_enabled_yes" if s["enabled"] else "server_enabled_no"
                ),
            )
            for i, s in enumerate(servers, 1)
        )
        return self._clip(self._tpl("servers", count=len(servers), items=items))

    async def _do_ping(self) -> str:
        """并发测试各中继节点的 TCP 连接延迟与可用性。"""
        try:
            data = await self._request("GET", "/api/servers/ping")
        except Exception as exc:  # noqa: BLE001
            return self._err_text(exc)
        rows = self._norm_ping(data)
        if not rows:
            return self._tpl("servers_none")
        items = "\n".join(
            self._tpl(
                "ping_item",
                index=i,
                name=row["name"],
                uri=row["uri"],
                latency=row["latency"],
            )
            for i, row in enumerate(rows, 1)
        )
        return self._clip(self._tpl("ping", items=items))

    async def _do_audit(self, event: AstrMessageEvent, tail: int) -> str:
        """查询最近操作记录。需管理员权限。

        wiki/rest-api.md：``/api/audit-logs?tail=N`` 返回
        ``{"logs": [{"ts": 1792059565, "kind": "login", "action": "登录成功", "ip": "..."}]}``
        （默认 100、上限 500、新→旧排列）。
        """
        denied = self._guard_admin(event)
        if denied:
            return denied
        rows = tail if tail and tail > 0 else self.audit_tail_default
        rows = max(1, min(int(rows), _AUDIT_MAX))
        try:
            data = await self._request("GET", "/api/audit-logs", params={"tail": rows})
        except Exception as exc:  # noqa: BLE001
            return self._err_text(exc)
        logs = data.get("logs") if isinstance(data, dict) else None
        logs = logs if isinstance(logs, list) else []
        if not logs:
            return self._tpl("audit_none")
        items = "\n".join(
            self._tpl(
                "audit_item",
                time=self._fmt_ts(entry.get("ts")),
                kind=entry.get("kind") or "-",
                action=entry.get("action") or "-",
                ip=entry.get("ip") or "-",
            )
            for entry in logs
            if isinstance(entry, dict)
        )
        return self._clip(self._tpl("audit", count=len(logs), items=items))

    # ======================================================================
    # 指令注册
    #   入口一：指令组「/astral <子指令>」（AstrBot 指令分组）
    #   入口二：扁平顶层「/ag<子指令>」（统一前缀，与需求清单一致）
    #   两者共用上面的 _do_* 逻辑，行为完全一致。
    #
    #   说明：AstrBot 指令别名的完整匹配名 = 父指令名 + 子指令名
    #   （见 CommandFilter.get_complete_command_names），因此子指令的 alias
    #   只能匹配「/astral ag<子指令>」这类形式，无法匹配裸「/ag<子指令>」，
    #   故这里为每个子指令额外注册一个顶层 handler。
    # ======================================================================

    @filter.command_group("astral")
    def astral(self):
        """AstralGame-Cli 远程管理指令组。"""
        pass

    # ---------------------------------------------------------- 1 状态查询

    @astral.command("status")
    async def astral_status(self, event: AstrMessageEvent):
        """查询 AstralGame-Cli 节点运行状态。"""
        yield event.plain_result(await self._do_status())

    @filter.command("agstatus")
    async def agstatus(self, event: AstrMessageEvent):
        """查询 AstralGame-Cli 节点运行状态。"""
        yield event.plain_result(await self._do_status())

    # ---------------------------------------------------------- 2 房间查询

    @astral.command("room")
    async def astral_room(self, event: AstrMessageEvent):
        """查询当前房间：房主、游戏、短码、分享链接与中继节点。"""
        yield event.plain_result(await self._do_room())

    @filter.command("agroom")
    async def agroom(self, event: AstrMessageEvent):
        """查询当前房间：房主、游戏、短码、分享链接与中继节点。"""
        yield event.plain_result(await self._do_room())

    # ---------------------------------------------------------- 3 日志查询

    @astral.command("logs")
    async def astral_logs(self, event: AstrMessageEvent, tail: int = 0):
        """查询运行日志（默认读取配置项 log_tail_default，上限 800 行）。需管理员权限。"""
        yield event.plain_result(await self._do_logs(event, tail))

    @filter.command("aglogs")
    async def aglogs(self, event: AstrMessageEvent, tail: int = 0):
        """查询运行日志（默认读取配置项 log_tail_default，上限 800 行）。需管理员权限。"""
        yield event.plain_result(await self._do_logs(event, tail))

    # ---------------------------------------------------------- 4 生命周期

    @astral.command("start")
    async def astral_start(self, event: AstrMessageEvent):
        """启动（或重启）EasyTier 实例。需管理员权限。"""
        yield event.plain_result(await self._do_start(event))

    @filter.command("agstart")
    async def agstart(self, event: AstrMessageEvent):
        """启动（或重启）EasyTier 实例。需管理员权限。"""
        yield event.plain_result(await self._do_start(event))

    @astral.command("stop")
    async def astral_stop(self, event: AstrMessageEvent):
        """停止当前实例。需管理员权限。"""
        yield event.plain_result(await self._do_stop(event))

    @filter.command("agstop")
    async def agstop(self, event: AstrMessageEvent):
        """停止当前实例。需管理员权限。"""
        yield event.plain_result(await self._do_stop(event))

    @astral.command("restart")
    async def astral_restart(self, event: AstrMessageEvent):
        """用最近一次配置重启实例。需管理员权限。"""
        yield event.plain_result(await self._do_restart(event))

    @filter.command("agrestart")
    async def agrestart(self, event: AstrMessageEvent):
        """用最近一次配置重启实例。需管理员权限。"""
        yield event.plain_result(await self._do_restart(event))

    # ---------------------------------------------------------- 5 创建房间

    @astral.command("create")
    async def astral_create(self, event: AstrMessageEvent, game_id: str, name: str = ""):
        """创建房间：create <游戏ID> [名称]。"""
        yield event.plain_result(await self._do_create(game_id, name))

    @filter.command("agcreate")
    async def agcreate(self, event: AstrMessageEvent, game_id: str, name: str = ""):
        """创建房间：create <游戏ID> [名称]。"""
        yield event.plain_result(await self._do_create(game_id, name))

    # ---------------------------------------------------------- 6 邀请链接

    @astral.command("invite")
    async def astral_invite(self, event: AstrMessageEvent):
        """读取当前房间的分享链接并回复；无房间时提示。"""
        yield event.plain_result(await self._do_invite())

    @filter.command("aginvite")
    async def aginvite(self, event: AstrMessageEvent):
        """读取当前房间的分享链接并回复；无房间时提示。"""
        yield event.plain_result(await self._do_invite())

    # ---------------------------------------------------------- 7 加入房间

    @astral.command("join")
    async def astral_join(self, event: AstrMessageEvent, invite: str):
        """加入房间：自动识别短码 / 完整链接 / AG1. 离线串并规范化后提交。"""
        yield event.plain_result(await self._do_join(invite))

    @filter.command("agjoin")
    async def agjoin(self, event: AstrMessageEvent, invite: str):
        """加入房间：自动识别短码 / 完整链接 / AG1. 离线串并规范化后提交。"""
        yield event.plain_result(await self._do_join(invite))

    # ---------------------------------------------------------- 8 离开房间

    @astral.command("leave")
    async def astral_leave(self, event: AstrMessageEvent):
        """离开房间：停止实例、清空当前房间。"""
        yield event.plain_result(await self._do_leave())

    @filter.command("agleave")
    async def agleave(self, event: AstrMessageEvent):
        """离开房间：停止实例、清空当前房间。"""
        yield event.plain_result(await self._do_leave())

    # ---------------------------------------------------------- 9 服务器列表

    @astral.command("servers")
    async def astral_servers(self, event: AstrMessageEvent):
        """列出已配置的中继服务器（含启用状态）。"""
        yield event.plain_result(await self._do_servers())

    @filter.command("agservers")
    async def agservers(self, event: AstrMessageEvent):
        """列出已配置的中继服务器（含启用状态）。"""
        yield event.plain_result(await self._do_servers())

    # ---------------------------------------------------------- 10 服务器测速

    @astral.command("ping")
    async def astral_ping(self, event: AstrMessageEvent):
        """并发测试各中继节点的 TCP 连接延迟与可用性。"""
        yield event.plain_result(await self._do_ping())

    @filter.command("agping")
    async def agping(self, event: AstrMessageEvent):
        """并发测试各中继节点的 TCP 连接延迟与可用性。"""
        yield event.plain_result(await self._do_ping())

    # ---------------------------------------------------------- 11 操作记录

    @astral.command("audit")
    async def astral_audit(self, event: AstrMessageEvent, tail: int = 0):
        """查询最近操作记录（默认读取配置项 audit_tail_default，上限 500 条）。需管理员权限。"""
        yield event.plain_result(await self._do_audit(event, tail))

    @filter.command("agaudit")
    async def agaudit(self, event: AstrMessageEvent, tail: int = 0):
        """查询最近操作记录（默认读取配置项 audit_tail_default，上限 500 条）。需管理员权限。"""
        yield event.plain_result(await self._do_audit(event, tail))

    # ---------------------------------------------------------- 12 WebSocket 状态

    @astral.command("ws")
    async def astral_ws(self, event: AstrMessageEvent):
        """查看 WebSocket 第三方服务通道的连接状态与推送配置。"""
        yield event.plain_result(await self._do_ws_status())

    @filter.command("agws")
    async def agws(self, event: AstrMessageEvent):
        """查看 WebSocket 第三方服务通道的连接状态与推送配置。"""
        yield event.plain_result(await self._do_ws_status())
