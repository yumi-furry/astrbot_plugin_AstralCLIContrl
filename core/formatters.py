"""消息格式化与响应结构归一化工具。

wiki 中部分接口（/api/games、/api/servers/ping、/api/ws/key、/api/update/check 等）
只描述了语义、未给出完整字段名，这里对可能的结构做了防御性归一化；
凡属推测处均以 TODO 标注，待文档确认后收敛。
"""

from __future__ import annotations

import json
import time
from typing import Any, List, Optional, Tuple

# ---------------------------------------------------------------- 通用


def mask_secret(s: str) -> str:
    """敏感字段脱敏：只显示后 4 位。"""
    s = str(s or "")
    if not s:
        return "(空)"
    return "****" + s[-4:] if len(s) > 4 else "****"


def fmt_ts(unix: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(unix)))
    except (TypeError, ValueError, OSError):
        return "?"


def fmt_uptime(secs: Any) -> str:
    if not isinstance(secs, (int, float)) or secs < 0:
        return "未知"
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def short_json(obj: Any, limit: int = 300) -> str:
    """未知结构的安全兜底展示。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------- 房间 / 成员


def is_relay_member(m: dict) -> bool:
    """判断成员是否为中继节点。

    TODO: 待文档确认 —— wiki 未说明 members 中中继节点的判定字段，
    这里按 hostname / conn_type 含 relay 字样做启发式过滤。
    """
    name = str(m.get("hostname") or "").lower()
    conn = str(m.get("conn_type") or "").lower()
    return "relay" in name or "relay" in conn


def fmt_members(members: Any, show_relay: bool = False, limit: int = 20) -> str:
    if not isinstance(members, list) or not members:
        return "  暂无成员"
    rows: List[str] = []
    hidden = 0
    for m in members:
        if not isinstance(m, dict):
            continue
        if is_relay_member(m) and not show_relay:
            hidden += 1
            continue
        name = m.get("hostname") or f"peer#{m.get('peer_id', '?')}"
        ip = m.get("ipv4") or "-"
        lat = m.get("latency_ms")
        lat_s = f"{lat} ms" if isinstance(lat, (int, float)) else "-"
        loss = m.get("loss_rate")
        loss_s = f"，丢包 {loss}" if isinstance(loss, (int, float)) and loss else ""
        conn = m.get("conn_type") or ""
        conn_s = f"（{conn}）" if conn and conn != "Local" else ""
        rows.append(f"  {name}（{ip}）延迟 {lat_s}{loss_s}{conn_s}")
        if len(rows) >= limit:
            rows.append(f"  ... 其余从略，共 {len(members)} 项")
            break
    if hidden:
        rows.append(f"  （已隐藏 {hidden} 个中继节点）")
    return "\n".join(rows) if rows else "  暂无成员"


def room_brief_lines(room: dict, share_url: str = "") -> List[str]:
    """房间关键信息（创建 / 加入成功后的回显）。"""
    lines: List[str] = []
    if not room:
        return ["  （服务器未返回房间详情）"]
    display = room.get("display_name") or room.get("game_name") or "未知"
    lines.append(f"  房间：{display}")
    if room.get("game_id"):
        lines.append(f"  类型：{room.get('game_name') or room['game_id']}（{room['game_id']}）")
    if room.get("short_code"):
        lines.append(f"  房间码：{room['short_code']}")
    if share_url:
        lines.append(f"  邀请链接：{share_url}")
    if room.get("offline_invite"):
        lines.append(f"  AG1 离线串：{room['offline_invite']}")
    return lines


def snapshot_lines(msg: Optional[dict]) -> List[str]:
    """由最近一帧事件消息生成状态摘要。"""
    if not msg:
        return ["节点状态：暂无数据（尚未收到事件推送）"]
    lines = [f"节点运行：{'是' if msg.get('running') else '否'}"]
    room = msg.get("room")
    if room:
        display = room.get("display_name") or room.get("game_name") or "?"
        lines.append(f"当前房间：{display}（房间码 {room.get('short_code') or '?'}）")
        if msg.get("share_url"):
            lines.append(f"邀请链接：{msg['share_url']}")
    else:
        lines.append("当前房间：无")
    node = msg.get("node") or {}
    extras = []
    if node.get("my_ipv4"):
        extras.append(f"虚拟 IP {node['my_ipv4']}")
    if node.get("total_nodes") is not None:
        extras.append(f"在线节点 {node['total_nodes']}")
    if node.get("server_version"):
        extras.append(f"服务端 v{node['server_version']}")
    if node.get("error"):
        extras.append(f"节点错误：{node['error']}")
    if extras:
        lines.append("节点信息：" + "，".join(str(x) for x in extras))
    return lines


def event_notify_text(event: str, msg: dict) -> str:
    """事件广播文案。snapshot 之外的 room/server 事件为关键变化。"""
    room = msg.get("room") or {}
    display = room.get("display_name") or room.get("game_name") or ""
    code = room.get("short_code") or "?"
    share = msg.get("share_url") or ""
    if event == "room.created":
        return f"【AstralGame】房间已创建：{display}，房间码 {code}\n{share}".strip()
    if event == "room.joined":
        return f"【AstralGame】已加入房间：{display}，房间码 {code}\n{share}".strip()
    if event == "room.left":
        return "【AstralGame】已离开房间，当前无房间。"
    if event == "server.started":
        tail = f"，已恢复房间 {code}" if room else "，当前无房间"
        return f"【AstralGame】服务已启动{tail}"
    if event == "snapshot":
        body = "；".join(snapshot_lines(msg))
        return f"【AstralGame】状态快照：{body}"
    return ""


# ---------------------------------------------------------------- 归一化（TODO 区）


def norm_games(data: Any) -> List[Tuple[str, str]]:
    """归一化游戏目录为 [(game_id, game_name)]。

    TODO: 待文档确认 —— wiki 仅注明「内置游戏目录（硬编码）」，
    未给出条目字段名，兼容 game_id/id 与 game_name/name 两种命名。
    """
    items = data
    if isinstance(data, dict):
        items = data.get("games") or data.get("items") or []
    out: List[Tuple[str, str]] = []
    if isinstance(items, list):
        for g in items:
            if not isinstance(g, dict):
                continue
            gid = g.get("game_id") or g.get("id") or g.get("gameId")
            gname = g.get("game_name") or g.get("name") or gid or ""
            if gid:
                out.append((str(gid), str(gname)))
    return out


def extract_key(data: Any) -> str:
    """从 /api/ws/key（或 rotate）响应中提取密钥。

    TODO: 待文档确认响应字段名，兼容 key / ws_key 与纯字符串响应。
    """
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for k in ("key", "ws_key", "token"):
            if data.get(k):
                return str(data[k])
    return short_json(data)


def extract_update_info(data: Any) -> dict:
    """归一化 /api/update/check 响应。

    TODO: 待文档确认字段名，兼容 current/latest/has_update/download_url 等常见命名。
    """
    d = data if isinstance(data, dict) else {}
    return {
        "current": d.get("current") or d.get("current_version") or d.get("version") or "?",
        "latest": d.get("latest") or d.get("latest_version") or d.get("tag_name") or "?",
        "has_update": d.get("has_update")
        if d.get("has_update") is not None
        else d.get("update_available"),
        "status": d.get("status") or d.get("message") or "",
        "download_url": d.get("download_url") or d.get("asset_url") or "",
        "raw": d,
    }


def find_download_url(info: dict) -> str:
    """从更新信息中挑选 release 资产下载地址（优先 linux + x86_64 命名）。"""
    url = info.get("download_url") or ""
    raw = info.get("raw") or {}
    assets = raw.get("assets") or []
    candidates: List[str] = []
    for a in assets if isinstance(assets, list) else []:
        if isinstance(a, dict):
            u = a.get("browser_download_url") or a.get("url") or ""
            if u:
                candidates.append(str(u))
        elif isinstance(a, str):
            candidates.append(a)
    for u in [url, *candidates]:
        if u and "linux" in u and "x86_64" in u:
            return u
    return url or (candidates[0] if candidates else "")


def norm_log_lines(data: Any) -> List[str]:
    """归一化 /api/logs 响应为日志行列表。

    TODO: 待文档确认响应字段名，兼容 logs / lines 与纯数组。
    """
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        for k in ("logs", "lines", "items"):
            if isinstance(data.get(k), list):
                return [str(x) for x in data[k]]
    if isinstance(data, str):
        return data.splitlines()
    return [short_json(data)]
