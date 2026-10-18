"""AstralGame 第三方服务 WebSocket 客户端。

职责：
- 维护与 ``/api/ws/service`` 端点的长连接（密钥鉴权，独立于 REST 的 Basic Auth）；
- 指数退避自动重连、应用层心跳（websockets ping）；
- 控制指令（get_room_info / open / close / join / restart）的请求-应答关联；
- 事件帧（snapshot / room.* / server.started / hello）分发给插件回调。

协议细节以 wiki/websocket-events.md 为准。
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlencode

from astrbot.api import logger


class WsNotConnectedError(Exception):
    """WS 未连接（未配置、断开重连中或已关闭）。"""


class WsCommandError(Exception):
    """服务器返回 ok=false（业务错误，如未开启 service_control_enabled）。"""


class AstralWsClient:
    def __init__(
        self,
        *,
        url: str,
        key: str,
        name: str = "AstrBot",
        service_type: str = "AstrBot",
        reconnect_interval: int = 5,
        reconnect_max_interval: int = 60,
        heartbeat_interval: int = 30,
        request_timeout: int = 10,
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
        log_traffic: bool = False,
    ) -> None:
        self._base_url = url
        self._key = key
        self._name = name
        self._type = service_type
        self._reconnect_interval = max(1, reconnect_interval)
        self._reconnect_max = max(self._reconnect_interval, reconnect_max_interval)
        self._heartbeat = max(0, heartbeat_interval)
        self._request_timeout = max(1, request_timeout)
        self._on_event = on_event
        self._log_traffic = log_traffic

        self._counter = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._ws: Any = None
        self._connected = asyncio.Event()
        self._stop = False
        self._task: Optional[asyncio.Task] = None

        self.last_hello: Optional[dict] = None  # 最近一次 hello 帧

    # ------------------------------------------------------------------ 属性

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动后台连接任务（幂等）。"""
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run(), name="astralgame-ws")

    async def stop(self) -> None:
        """优雅关闭：停止重连、关闭连接、失败所有挂起请求。"""
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._fail_all_pending(WsNotConnectedError("连接已关闭"))

    async def reload(self, **params: Any) -> None:
        """按新参数重建连接（用于配置变更 / ws_key 轮换后）。"""
        mapping = {
            "url": "_base_url",
            "key": "_key",
            "name": "_name",
            "service_type": "_type",
            "reconnect_interval": "_reconnect_interval",
            "reconnect_max_interval": "_reconnect_max",
            "heartbeat_interval": "_heartbeat",
            "request_timeout": "_request_timeout",
        }
        for k, v in params.items():
            if k in mapping and v is not None:
                setattr(self, mapping[k], v)
        await self.stop()
        self.start()

    # ---------------------------------------------------------------- 主循环

    def _full_url(self) -> str:
        sep = "&" if "?" in self._base_url else "?"
        return self._base_url + sep + urlencode(
            {"key": self._key, "name": self._name, "type": self._type}
        )

    async def _run(self) -> None:
        import websockets  # 延迟导入，未安装时仅在首次连接报错

        backoff = self._reconnect_interval
        while not self._stop:
            try:
                async with websockets.connect(
                    self._full_url(),
                    ping_interval=self._heartbeat or None,  # 0 表示禁用心跳
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = self._reconnect_interval  # 连接成功后重置退避
                    logger.info("[AstralGame] WS 已连接")
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[AstralGame] WS 连接异常：{e}")
            finally:
                self._ws = None
                self._connected.clear()
                self._fail_all_pending(WsNotConnectedError("WS 连接断开，等待重连"))
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max)  # 指数退避，封顶

    # ---------------------------------------------------------------- 帧分发

    async def _dispatch(self, raw: Any) -> None:
        if self._log_traffic:
            logger.debug(f"[AstralGame] WS 收帧：{raw}")
        try:
            msg = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning(f"[AstralGame] 无法解析的 WS 帧：{raw!r}")
            return

        # 指令应答：按 id 关联到挂起的 Future
        if msg.get("event") == "result":
            rid = msg.get("id")
            fut = self._pending.pop(rid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        if msg.get("event") == "hello":
            self.last_hello = msg
            logger.info(f"[AstralGame] WS 握手成功：{msg}")

        if self._on_event is not None:
            try:
                await self._on_event(msg)
            except Exception as e:
                logger.warning(f"[AstralGame] 事件回调异常：{e}")

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ---------------------------------------------------------------- 控制指令

    async def request(self, action: str, timeout: Optional[int] = None, **fields: Any) -> Any:
        """发送控制指令并等待 ``event=result`` 应答。

        - 未连接抛 ``WsNotConnectedError``；
        - 超时抛 ``TimeoutError``；
        - 服务器返回 ok=false 抛 ``WsCommandError``（消息为服务端错误原文）。
        """
        if not self.connected or self._ws is None:
            raise WsNotConnectedError("未连接到 AstralGame 服务器（WS 断开或重连中）")
        rid = f"req-{next(self._counter)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        try:
            payload = {"id": rid, "action": action, **fields}
            if self._log_traffic:
                logger.debug(f"[AstralGame] WS 发帧：{payload}")
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            msg = await asyncio.wait_for(fut, timeout or self._request_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("服务器无响应（WS 指令超时）") from None
        finally:
            self._pending.pop(rid, None)

        if not msg.get("ok"):
            raise WsCommandError(msg.get("error") or "未知错误")
        return msg.get("data")
