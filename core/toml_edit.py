"""极简 TOML 读写工具（行级处理，不依赖第三方解析库）。

仅覆盖本插件所需场景：
- 按点分键（如 ``astral_server.ws_key``）读取标量值；
- 按点分键写入标量值（布尔 / 整数 / 浮点 / 字符串自动识别类型）。

限制（对本插件用途可接受）：
- 不处理多行字符串、内联表、数组的复杂编辑（数组原样透传）；
- 节头按 ``[a.b]`` 字面匹配。
"""

from __future__ import annotations

import re
from typing import List, Optional

_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_\-]+)\s*=")


def _split_dotted(dotted: str) -> tuple[List[str], str]:
    parts = [p.strip().strip('"') for p in dotted.split(".") if p.strip()]
    return parts[:-1], (parts[-1] if parts else "")


def _parse_section(header: str) -> List[str]:
    return [s.strip().strip('"') for s in header.split(".")]


def toml_get(text: str, dotted: str) -> Optional[str]:
    """读取点分键对应的原始值文本（未找到返回 None）。"""
    section, key = _split_dotted(dotted)
    if not key:
        return None
    cur: List[str] = []
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            cur = _parse_section(m.group(1))
            continue
        km = _KEY_RE.match(line)
        if km and cur == section and km.group(2) == key:
            return line.split("=", 1)[1].strip()
    return None


def format_value(raw: str) -> str:
    """把用户输入的字符串格式化为合法 TOML 标量。"""
    s = raw.strip()
    if re.fullmatch(r"(?i)(true|false)", s):
        return s.lower()
    if re.fullmatch(r"-?\d+", s):
        return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        return s
    if (s.startswith('"') and s.endswith('"') and len(s) >= 2) or (
        s.startswith("[") and s.endswith("]")
    ):
        return s  # 用户已自行书写为 TOML 字面量，原样透传
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


def toml_set(text: str, dotted: str, raw_value: str) -> str:
    """设置点分键的值，返回新的 TOML 文本。

    已存在则替换该行；键不存在则插入对应节末尾；节不存在则新建节。
    顶级键（无节）插入到第一个节头之前。
    """
    section, key = _split_dotted(dotted)
    if not key:
        raise ValueError("配置键不能为空")
    value = format_value(raw_value)
    lines = text.splitlines()

    header_idx: Optional[int] = None
    cur: List[str] = []
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            cur = _parse_section(m.group(1))
            if cur == section:
                header_idx = i
                break

    if section and header_idx is None:
        # 节不存在：整体追加新节
        lines.append(f"[{'.'.join(section)}]")
        lines.append(f"{key} = {value}")
        return "\n".join(lines) + "\n"

    # 扫描目标节范围 [start, end)
    start = (header_idx + 1) if header_idx is not None else 0
    end = len(lines)
    for i in range(start, len(lines)):
        if _SECTION_RE.match(lines[i]):
            end = i
            break

    for i in range(start, end):
        km = _KEY_RE.match(lines[i])
        if km and km.group(2) == key:
            indent = km.group(1)
            comment = ""
            # 保留行尾注释
            if " #" in lines[i]:
                comment = " #" + lines[i].split(" #", 1)[1]
            lines[i] = f"{indent}{key} = {value}{comment}"
            return "\n".join(lines) + "\n"

    # 键不存在：插入节末尾
    lines.insert(end, f"{key} = {value}")
    return "\n".join(lines) + "\n"
