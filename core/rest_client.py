"""AstralGame REST API 客户端（Basic Auth 鉴权，独立于 WS 通道的 ws_key）。

仅用于 WS 控制协议没有覆盖的管理功能：配置、凭据、服务器列表、游戏目录、
WS 密钥管理、SMTP、在线更新、节点日志等。端点以 wiki/rest-api.md 为准。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import aiohttp


class RestError(Exception):
    """REST 请求失败。status 为 HTTP 状态码（连接层失败时为 None）。"""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        self.status = status
        super().__init__(message)


class RestClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=max(1, timeout))
        # 用户名留空时服务端关闭鉴权，此时不带 Authorization 头
        self._auth = aiohttp.BasicAuth(username, password) if username else None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                timeout=self._timeout,
                headers={"User-Agent": "astrbot-plugin-astralgame"},
            )
        return self._session

    async def request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        params: Optional[dict] = None,
    ) -> Any:
        sess = await self._sess()
        try:
            async with sess.request(
                method, self.base_url + path, json=json_body, params=params
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # 错误响应统一为 {"error": "..."}（见 rest-api.md）
                    msg: str = text
                    try:
                        msg = json.loads(text).get("error", text)
                    except (TypeError, json.JSONDecodeError, AttributeError):
                        pass
                    raise RestError(str(msg), status=resp.status)
                try:
                    return json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    return text  # 部分接口可能返回纯文本
        except aiohttp.ClientError as e:
            raise RestError(f"连接失败：{e}") from e

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
        return await self.request("POST", path, json_body=body, params=params)

    async def put(self, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
        return await self.request("PUT", path, json_body=body, params=params)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
