# Astral_CLI_contrl

AstrBot 插件：通过聊天指令远程管理 **AstralGame-Cli** 联机服务。

- 指令分组：`/astral <子指令>`；同时**独立注册**扁平顶层指令 `/ag<子指令>`（两种写法等价，见下表）。
  - 注：AstrBot 指令别名的完整匹配名 = 父指令名 + 子指令名（见 `CommandFilter.get_complete_command_names`），
    子指令的 `alias` 只能匹配 `/astral ag<子指令>`，无法匹配裸 `/ag<子指令>`，因此扁平入口单独注册。
- 通道：仅使用 **REST API**（鉴权采用官方 wiki 推荐的 HTTP Basic Auth）。
- WebSocket 实时推送 / 第三方服务控制指令为**第二阶段**，本版本未启用，扩展说明见文末。

> 所有端点、请求方法、请求体字段、响应结构、鉴权方式、房间对象字段、邀请码格式均以 AstralGame-Cli 官方 wiki 为准：
> `wiki/rest-api.md`、`wiki/rooms.md`、`wiki/websocket-events.md`。
> wiki 未明确之处，代码中以 `# TODO:` 标注假设。

## 目录结构

```
astrbot_plugin_astralgame/          # 放入 AstrBot 的 data/plugins/ 下
├── main.py                         # 插件主逻辑（13 个指令 + 统一请求封装）
├── metadata.yaml                   # 插件元信息（AstrBot v4 的元数据来源）
├── requirements.txt                # 依赖：aiohttp
├── _conf_schema.json               # 配置项定义（含全部可配置回复文案）
├── README.md                       # 本文件
└── core/                           # 预置模块（本阶段未接线）
    ├── rest_client.py              # REST 客户端（备用封装）
    ├── ws_client.py                # 第二阶段：第三方服务 WS 客户端
    ├── formatters.py               # 结构化字段归一化辅助
    └── toml_edit.py                # 轻量 TOML 读写
```

> 说明：`main.py` 为自包含实现（自带 `_request` 统一封装与渲染逻辑），不依赖 `core/`，
> 以确保在任何加载方式下都能直接运行。`core/ws_client.py` 预留给第二阶段的 WebSocket 接入。

## 安装

1. 将插件目录放入 AstrBot 的 `data/plugins/` 下（目录名保持 `astrbot_plugin_astralgame`）。
2. 在 AstrBot WebUI 的 **插件** 页安装依赖，或手动执行：
   ```bash
   pip install -r requirements.txt
   ```
3. 在插件卡片点击 **重载插件**，使插件生效。
4. 打开插件配置，填写 `base_url` 与鉴权凭据（见下一节）。

> 元数据由 `metadata.yaml` 提供；`main.py` 中的 `@register(...)` 仅为兼容旧版 AstrBot 的写法，
> 在新版本中导入失败会自动降级为空实现，不影响加载。

## 配置（`_conf_schema.json`）

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `base_url` | string | `http://127.0.0.1:8080` | AstralGame-Cli 服务地址，插件在其后拼接 `/api` 业务接口 |
| `username` | string | 空 | Basic Auth 用户名；**留空表示服务端未开启鉴权** |
| `password` | string | 空 | Basic Auth 密码（面板中以密文展示） |
| `timeout` | int | `10` | 单次 REST 请求超时（秒） |
| `log_tail_default` | int | `100` | `/astral logs` 未指定行数时的默认值（服务端上限 800） |
| `audit_tail_default` | int | `20` | `/astral audit` 未指定条数时的默认值（服务端上限 500） |
| `max_output_chars` | int | `3000` | 单条消息最大字符数，超过则截断并提示总条数 |
| `messages` | object | 见下 | 全部可配置回复文案 |

鉴权：采用 wiki 推荐的 `Authorization: Basic base64(username:password)`；用户名留空时不发送该头。

## 指令一览

| 指令（分组写法） | 等价扁平写法 | 权限 | 说明 |
|---|---|---|---|
| `/astral status` | `/agstatus` | 普通 | 查询节点运行状态、当前房间与节点信息 |
| `/astral room` | `/agroom` | 普通 | 查询当前房间：房主、游戏、短码、分享链接、中继节点；无房间时提示 |
| `/astral logs [行数]` | `/aglogs` | 管理员 | 查询运行日志，默认取配置 `log_tail_default`；过长时截断并提示总行数 |
| `/astral start` | `/agstart` | 管理员 | 启动（重启）EasyTier 实例 |
| `/astral stop` | `/agstop` | 管理员 | 停止当前实例 |
| `/astral restart` | `/agrestart` | 管理员 | 用最近一次配置重启实例 |
| `/astral create <游戏ID> [名称]` | `/agcreate` | 普通 | 创建房间，成功后回复短码（不可用时回退离线串）与分享链接 |
| `/astral invite` | `/aginvite` | 普通 | 读取并回复当前房间分享链接；无房间时提示 |
| `/astral join <邀请码>` | `/agjoin` | 普通 | 自动识别三种邀请格式并规范化后加入 |
| `/astral leave` | `/agleave` | 普通 | 离开房间（停止实例、清空当前房间） |
| `/astral servers` | `/agservers` | 普通 | 列出已配置的中继服务器（含启用状态） |
| `/astral ping` | `/agping` | 普通 | 并发测试各中继节点延迟与可用性 |
| `/astral audit [条数]` | `/agaudit` | 管理员 | 查询最近操作记录，默认取配置 `audit_tail_default` |

权限校验 API：`AstrMessageEvent.is_admin()`（等价于 `event.role == "admin"`）。
校验失败回复配置项 `messages.msg_no_permission`（默认「权限不足」）。

### 用法示例

两种写法等价，任选其一：

```
# 分组写法
/astral status
/astral room
/astral logs 200
/astral create minecraft 我的世界
/astral invite
/astral join ABC123
/astral join https://next.astral.fan/j?c=ABC123
/astral join AG1.xxxxx…
/astral servers
/astral ping
/astral audit 20
/astral restart

# 扁平写法（统一前缀）
/agstatus
/agroom
/aglogs 200
/agcreate minecraft 我的世界
/aginvite
/agjoin ABC123
/agjoin AG1.xxxxx…
/agservers
/agping
/agaudit 20
/agrestart
```

## 邀请码规范化（`normalize_invite`）

严格对齐 `wiki/rooms.md` 的三种邀请形式：

| 形式 | 示例 | 处理 |
|---|---|---|
| 短码 | `ABC123` | 去空白 / 连字符 / 下划线 → 转大写 → `I/L→1`、`O→0` |
| 完整邀请链接 | `https://next.astral.fan/j?c=ABC123` | 从 URL 的 `c` 参数提取令牌（可能是短码或 `AG1.` 串），再按规则处理 |
| `AG1.` 离线串 | `AG1.xxxxx…` | 自包含，原样返回（大小写敏感，不做去歧义替换） |

## 可配置文案与占位符

所有回复内容均位于配置项 `messages` 下，可自由改写。`{xxx}` 为占位符，动态替换；未知占位符原样保留，不会报错。

| 配置键 | 占位符 | 用途 |
|---|---|---|
| `msg_no_permission` | — | 权限不足提示 |
| `msg_error` | `{error}` | 通用失败提示 |
| `msg_unauthorized` | — | 401 鉴权失败提示 |
| `msg_room_empty` | — | 未在房间中提示 |
| `msg_bad_invite` | — | 邀请码无效提示 |
| `msg_truncated` | `{shown}` `{total}` | 长文本截断提示 |
| `status` | `{running}` `{room_line}` `{node_line}` | `/astral status` 回复 |
| `status_running_yes` / `status_running_no` / `status_unknown` | — | 运行状态取值文案 |
| `room_none_short` | — | 无房间（短） |
| `room_summary` | `{game_name}` `{short_code}` | 状态 / 操作行内的房间摘要 |
| `room` | `{is_host}` `{game_name}` `{game_id}` `{display_name}` `{short_code}` `{share_url}` `{offline_invite}` `{peers}` | `/astral room` 回复 |
| `room_peers_none` / `room_peer_item` | `{uri}` | 中继节点区及其条目 |
| `logs` / `logs_none` | `{count}` `{lines}` | 日志回复 |
| `start_ok` / `start_fail` | `{status_line}` / `{error}` | 启动结果 |
| `stop_ok` / `stop_fail` | — / `{error}` | 停止结果 |
| `restart_ok` / `restart_fail` | `{status_line}` / `{error}` | 重启结果 |
| `create_ok` / `create_fail` | `{game_name}` `{game_id}` `{short_code}` `{share_url}` `{offline_invite}` / `{error}` | 创建结果 |
| `invite_ok` | `{share_url}` `{short_code}` | 分享链接 |
| `join_ok` / `join_fail` | `{game_name}` `{game_id}` `{short_code}` `{share_url}` / `{error}` | 加入结果 |
| `leave_ok` / `leave_fail` | — / `{error}` | 离开结果 |
| `servers` / `servers_none` / `server_item` | `{count}` `{items}` / — / `{index}` `{name}` `{uri}` `{enabled}` | 服务器列表 |
| `server_enabled_yes` / `server_enabled_no` | — | 启用状态取值文案 |
| `ping` / `ping_item` | `{items}` / `{index}` `{name}` `{uri}` `{latency}` | 服务器测速 |
| `audit` / `audit_none` / `audit_item` | `{count}` `{items}` / — / `{time}` `{kind}` `{action}` `{ip}` | 操作记录 |

## 与 wiki 的差异与 TODO

以下条目在 wiki 中未给出明确字段名，代码已用防御性归一化并在源码中以 `# TODO:` 标注假设：

- `/api/status` 快照字段名（按事件消息中的 `running` / `node.*` 兼容取值）。
- `/api/games` 目录条目字段名（兼容 `game_id/id` 与 `game_name/name`）。
- `/api/room/create` 与 `/api/room/join` 的响应结构（兼容 `{"room": {...}, "share_url": ...}` 与直接返回房间对象）。
- `/api/servers` 条目字段名（据请求体推断为 `{id, name, uri, enabled}`）。
- `/api/servers/ping` 响应结构（兼容 `list[dict]` 与 `dict{uri: ms}`，耗时字段兼容多种命名）。
- `/api/logs` 响应字段名（兼容纯数组 / `logs|lines|items` / 纯文本）。
- 「总行数」：wiki 未定义独立字段，`/astral logs` 的「总行数」以**服务端返回的日志条数**为准。

## 后续扩展（第二阶段：WebSocket 实时通知）

`core/ws_client.py` 已预留 `AstralGame` 第三方服务 WebSocket 客户端（`/api/ws/service?key=<密钥>&name=&type=`，密钥鉴权）。
第二阶段计划接入：

- 连接建立后接收 `snapshot` 与 `hello`，实时订阅 `room.created` / `room.joined` / `room.left` / `server.started` / `audit` 事件，并向指定会话主动推送；
- 发送控制指令（`get_room_info` / `open` / `close` / `join` / `restart`）——其中控制类指令需服务端开启 `service_control_enabled`；
- 借助 `get_room_info` 的 `members`（含 `latency_ms` / `loss_rate`）补齐 `/astral room` 的成员列表与延迟展示。

密钥可在面板「设置 → 第三方服务」查看 / 刷新；刷新后旧密钥立即失效。

## License / 致谢

本插件基于 AstralGame-Cli 官方 API 开发；接口定义版权归原项目所有。
