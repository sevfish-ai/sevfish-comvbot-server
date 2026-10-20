# SevFishComVBot

**把企业微信变成可编程的 AI 员工入口。**

[![CI](https://github.com/sevfish-ai/sevfish-comvbot-server/actions/workflows/ci.yml/badge.svg)](https://github.com/sevfish-ai/sevfish-comvbot-server/actions/workflows/ci.yml)

> 徽章指向 `sevfish-ai` 组织。
 一个跑在你自己的服务器上的 AI 客服/运营系统，搭配一个装在企业微信所在设备（平板或手机）上的执行端。

> English: An open-source, self-hosted AI agent platform for WeCom (WeChat Work). The server runs LLM agents, long-term memory, workflows and scheduled tasks; the Android client drives a real WeCom account through Android Accessibility.

- 不依赖企业微信官方 API，用**真实账号**收发消息（官方外部联系人接口拿不到的群聊上下文，这里能拿到）
- 数据、密钥、模型全部在**你自己的机器**上
- 代码 Apache-2.0，可以自建、可以改、可以商用（保留商标）

---

## 它由两部分组成

| | 仓库 | 跑在哪 | 干什么 |
|---|---|---|---|
| 服务端 | [`sevfish-comvbot-server`](../sevfish-comvbot-server) | Linux / Docker | WebSocket 接入、智能体、长期记忆、工作流、任务调度、知识库、数据表格、管理后台、客户控制台 |
| 客户端 | [`sevfish-comvbot-app`](.) | 一台 Android 平板（推荐）或手机，与企微同机 | 通过无障碍服务操作真实企业微信：读消息、进房间、发消息、加好友、拉群 |

**客户端不含业务逻辑**。它把"看到了什么"上报给服务端，把服务端下发的指令变成点击和输入；自动回复、记忆、决策全在服务端。所以：**协议才是产品**（见 [PROTOCOL](PROTOCOL.md)）。换掉客户端，只要实现同一份协议，服务端照常工作。

```
 企业微信(真机)  ←无障碍→  SevFishComVBot App  ←WebSocket→  你的服务器  ←→  LLM / 数据库
```

---

## 能做什么

- **自动回复 v2**：外部群 / 外部私聊 / 内部群 / 内部私聊分别配置"回 / 只 @ 才回 / 持续跟进 / 静默"，同一房间同一人的多条消息合并后一次交给模型
- **智能体**：可联网、可查知识库、可读写表格、可建任务、可下发 20+ 种企业微信指令，按指令逐个授权
- **长期记忆**：按客户维度记事实（去重、别名自愈、按设备挂载、多设备共享），可定时让模型压缩合并
- **工作流**：显式节点图（LLM / 表格 / HTTP / 设备指令 / 条件 / 分支 / 代码 / 输出），画布编辑
- **任务**：cron、间隔、一次性、好友通过、关键词触发 → 跑工作流或智能体
- **知识库 + 数据表格**：上传即检索；表格可建可查可导出 CSV
- **管理后台**：组织、成员、设备、回复策略、智能体、知识库、记忆、表格、工作流、任务、技能、安全审计
- **客户控制台** `/ext`：给最终客户用的只读控制台（改模型、开关推送、维护记忆），走隧道 + 三道门保护
- **AI 配置组织**：管理员用自然语言描述需求，模型自己把组织/智能体/表格/工作流/知识库/任务/设备建出来

---

## 快速开始

```bash
# 服务端：Linux + MySQL 8 + Python 3.14（不需要 Docker）
git clone https://github.com/sevfish-ai/sevfish-comvbot-server.git && cd sevfish-comvbot-server
python3 -m venv ../venv && ../venv/bin/pip install -r fastapi/requirements.txt
cp .env.example .env && $EDITOR .env      # 填 MYSQL_* 与 SITE_URL
cd fastapi && ../venv/bin/python run.py    # 前台先跑通；长期运行用 systemd（见 06）
# 浏览器打开 http://<你的IP>:8765/admin/orgs

# 客户端（需要 JDK 8/11 + Android SDK，产物是 debug APK）
git clone https://github.com/sevfish-ai/sevfish-comvbot-app.git && cd sevfish-comvbot-app
./gradlew assembleDebug
```

想用 Docker 一条命令起（含 MySQL，未生产验证）：`cp .env.example .env && docker compose up -d`。

装 APK → 开无障碍服务 → 填服务器地址 → 设备列表出现 → 建智能体 → 发一条消息试试。

详细步骤见 [03-快速开始](docs/03-快速开始.md)。

---

## 文档

| 文档 | 内容 |
|---|---|
| [01-项目介绍](docs/01-项目介绍.md) | 解决什么问题、适合谁、不适合谁 |
| [02-架构与组件](docs/02-架构与组件.md) | 分层、服务、数据表、消息流 |
| [03-快速开始](docs/03-快速开始.md) | 30 分钟从零到第一条自动回复 |
| [04-核心能力](docs/04-核心能力.md) | 回复策略、智能体、记忆、工作流、任务、知识库 |
| [05-通信协议](docs/05-通信协议.md) | WebSocket 事件与指令契约（也见 [PROTOCOL.md](PROTOCOL.md)） |
| [06-自建部署](docs/06-自建部署.md) | 生产部署、配置项、备份、升级、回滚 |
| [07-安全与边界](docs/07-安全与边界.md) | 威胁模型、默认安全、必做加固、合规提醒 |
| [08-FAQ](docs/08-FAQ.md) | 常见问题 |
| [09-客户端构建](docs/09-客户端构建.md) | 环境、编译、安装、已移除的闭源 SDK |

---

## 边界与免责

- 这是一个**自动化工具**，通过安卓无障碍模拟人在企业微信里操作。它不代表企业微信官方，也不提供任何"防封"保证；账号风险由使用者承担。
- 服务端默认只监听内网，`/ext` 在未配置隧道密钥时整体 403。**不要把管理后台直接暴露到公网。**
- 开源版本移除了原商业版本中的第三方闭源 SDK（腾讯 WWAPI、友盟、TalkingData 统计），相应能力为存根实现。
- 商标 "SevFish / SevFishComVBot" 不随代码授权（Apache-2.0 §6）；你可以把代码用于商业项目，但发布衍生版本请换名字。

## License

[Apache-2.0](LICENSE)。
