# 05 · 通信协议

服务端与客户端之间只有这一层耦合：**WebSocket + JSON**。它定义得足够小，所以客户端可以被替换——只要实现下面的事件与指令，任何能操作企业微信的程序都能接进来。

## 1. 连接

```
ws://<host>:8765/webserver/wework/{client_id}
```

- `client_id` = 设备号（服务端 `robots` 表里的设备标识），同时也是设备的身份：**没有鉴权 token**，因此服务端必须只暴露在内网/隧道后面（见 07）。
- 附加路径（兼容）：`/ws/{client_id}`、`/ws`。
- 同一设备允许多条连接并存；`active_connections` 是唯一在线事实来源。

## 2. 心跳

```jsonc
// 客户端 → 服务端（裸消息，不带信封）
{"type": 11}
// 服务端 → 客户端
{"type": 11, "timestamp": "2026-01-01T12:00:00"}
```

超时与间隔：`WS_HEARTBEAT_INTERVAL` / `WS_HEARTBEAT_TIMEOUT`。

> 客户端还有一种传输层探针 `{"td": <ts>}`，服务端直接丢弃，无业务含义。

## 3. 信封

**服务端下发指令**

```jsonc
{
  "socketType": 2,
  "messageId": "server_20260101120000_deadbeef",
  "encryptType": 0,
  "list": [{"type": 203, "titleList": "某客户群", "receivedContent": "在的，您说"}]
}
```

**客户端上报**（`socketType` 区分两类）

| socketType | 含义 | 载荷字段 |
|---|---|---|
| `2` | 数据上报（消息列表、查询结果） | `list[]`，每项带 `type` |
| `3` | 执行回执 | `list[]`，每项 `errorCode` / `errorReason` / `successList` / `failList` / `rawMsg` |

## 4. 客户端 → 服务端：事件

| type | 名称 | 说明 |
|---|---|---|
| `101` | 聊天消息 | `messageList[]`、`titleList[]`（房间）、`roomType`、发言者、正文。**@ 信息不在其中**，只能从正文识别 |
| `302` | 设备错误日志 | `GlobalMethod.error()` 上抛，进「功能测试」页的设备错误日志。排查"进不去房间/发不出消息"看这里 |
| `501` | 群详情返回 | 群名、公告、成员 |
| `502` | 好友资料返回 | |
| `503` | 我的信息返回 | 含**本机企微名**（群聊 @ 判定依赖它，缓存在服务端内存） |
| `505` | 最近聊天返回 | |
| `506` | 企业列表返回 | |
| `507` | 全部好友返回 | 任务的条件触发靠它做 diff |
| `508` | 全部群返回 | |
| `512` | 任务/指令执行完成 | 指令 201/202 的最终状态以它为准 |

`roomType` 有效值：`1` 外部群 / `2` 外部私聊 / `3` 内部群 / `4` 内部私聊。（`app/models/message.py` 里有个早期的两值枚举，已过时，别照它写。）

## 5. 服务端 → 客户端：指令

| type | 指令 | 必填参数 |
|---|---|---|
| `201` | 停止接收 | — |
| `202` | 开始接收 | — |
| `203` | 发送消息 | `titleList` `receivedContent` `textType`；可选 `at` `atList` |
| `204` | 指定回复(@) | `titleList` `receivedName` `originalContent` `textType` `receivedContent` |
| `205` | 转发消息 | `titleList` `receivedName` `originalContent` `textType` `nameList`；可选 `extraText` |
| `206` | 创建群 | `groupName` `selectList`；可选 `groupAnnouncement` `groupRemark` `groupTemplate` |
| `207` | 修改群配置 | `groupName`；可选 `newGroupName` `newGroupAnnouncement` `selectList` `removeList` `groupRemark` `groupTemplate` |
| `213` | 改备注 / 按手机号加好友 | 双用法：改备注 `friend.name` + `markName`；加好友 `friend.phone` + 备注/附言 |
| `218` | 推送文件 | 文件路径/URL 与目标 |
| `219` | 解散群 | `groupName` |
| `220` | 从外部群添加好友 | 群名 + 成员 |
| `221` | 添加待办 | 标题/内容/时间 |
| `222` | 打卡 | — |
| `223` | 切换企业 | 企业名 |
| `224` | 推送链接 | 链接 + 标题 + 描述 |
| `225` | 修改群成员信息 | 群名 + 成员 + 字段 |
| `226` | 撤回消息 | 目标消息 |
| `233` | 扫一扫 | — |
| `234` | 删除联系人 | 姓名/备注 |
| `501`~`509` | 各类读取 | 群详情 / 好友资料 / 我的信息 / 最近聊天 / 企业列表 / 全部好友 / 全部群 / 本地文件 |

参数契约的权威定义在服务端 `app/services/agent_service.py` 的 `_CMD_PARAM_SCHEMA`（执行前会校验，缺必填直接拒绝），文案在 `_CMD_LABELS`。工作流画布的指令选项与它保持一致。

### 203 的语义细节

- `titleList` 是**房间名**（群名，或私聊对方的会话标题）。设备端按"列表行标题"精确匹配后进房，不是靠搜索。
- `textType`：`1` 文本 / `2` 图片 / `3` 文件。
- `atList`：要 @ 的成员名，逗号分隔；`at` 用于 @所有人。
- **可选 `roomType`**：只有自动回复链路会带（类型来自设备 101 + 外部群纠偏），用于设备端拒绝"类型不符"的发送。工作流/任务/测试按钮**不带**，设备端 `expectType=0` 一律放行。别把配置里"猜"的类型发下去——那会让"往私聊发消息"的任务被设备按类型不符误拒。

## 6. 回执语义

```jsonc
{"socketType": 3, "list": [{"errorCode": 0, "successList": ["某客户群"], "failList": [], "rawMsg": "..."}]}
```

- `errorCode == 0` 只代表"指令被接受"，**发送是否成功看 `successList`/`failList`**。
- 目标房间不在当前可见列表、且搜索也进不去 → 进 `failList` 并带原因，**绝不会"猜一个"发出去**（同名联系人/同族群的误发是真实踩过的坑）。

## 7. 版本协商（约定）

协议按整数版本演进。规划中的握手：连接时带查询参数 `?pv=<版本>`，服务端在不兼容时以 close code `4001` 断开，客户端应提示"客户端版本过旧"。

当前实现以"字段可选 + 缺省放行"保证向后兼容（新字段老客户端忽略、老指令新服务端照发）。改动必须遵守：

1. **只增字段，不改语义**；要改语义就换指令号。
2. 新增指令号段从 `240` 起，避开已用区间。
3. 任何"设备可能不实现"的能力，服务端必须能降级（超时/失败都当作不支持继续跑）。

## 8. 自己实现一个客户端

最小可行实现只需要：

1. 连上 `/webserver/wework/{client_id}`，每 30s 发 `{"type": 11}`；
2. 收到 `socketType=2` 且 `list[0].type == 203` 时执行发送，回一条 `socketType=3` 的回执；
3. 看到新的群/私聊消息时上报 `socketType=2` + `list[{type:101, ...}]`。

其余指令可以先全部回 `errorCode=1`，服务端不会因此罢工（工具调用会失败，但不影响自动回复主链路）。
