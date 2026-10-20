# 维护说明

> 给未来的维护者（很可能就是你自己）。这个项目刻意选择"低维护"：能跑就别动。

## 维护承诺

- bug 修；企业微信改版导致的定位失效修；安全问题修。
- 新功能按需求与精力接，不承诺路线图。
- 不做：多平台适配层、云托管服务、官方 APK 分发。

## 改代码前必读的三条不变式

1. **`RELOAD` 必须为 false**。uvicorn 热重载会重建模块，内存里的设备连接注册表被清空 → 全部设备假掉线。调试请改完手动重启。
2. **在线状态唯一事实来源是 `active_connections`**，不是 `robots.status` 列。DB 列只是"最后一次连接事件"，多条路径不会回写。管理页、`/api/status` 都按注册表判。
3. **表自增值启动时抬到 1000**（新 id 从 1000 起）。测试别断言 `id=1`。

## 企业微信改版时的排查顺序

1. 在平板（或手机）上抓界面：`adb shell uiautomator dump /sdcard/ui.xml && adb pull /sdcard/ui.xml`
2. `python3 tools/parse_ui.py ui.xml` 看节点树与 `resource-id`
3. 对照 `service/WeworkOperationImpl.kt`（操作）与 `utils/WeworkRoomUtil.kt`（房间判定）
4. 真机验证用服务端仓的 `fastapi/tests/manual_room_send.py`（通过服务端 HTTP 驱动真机，不需要 adb 交互）

## 发版流程

```bash
# 1. 服务端自检（需要本地 MySQL；先建虚拟环境并 pip install -r requirements.txt）
cd fastapi && python -m pytest tests/ -q
# 2. 起一遍容器，确认 compose 能跑
docker compose up -d && curl -s localhost:8765/api/status
# 3. 打 tag（客户端与服务端 tag 不必同步）
git tag vX.Y.Z && git push --tags
```

客户端仓单独发版：`cd sevfish-comvbot-app && ./gradlew assembleDebug`（JDK 8/11）。
协议不兼容时在 `PROTOCOL.md` 顶部记录变更，并更新 README 里的兼容版本范围。

> 本仓是从内部项目生成的快照。`docs/`、`README.md`、`PROTOCOL.md`、`Dockerfile` 等都来自生成脚本，
> 直接改这里的文档没问题，但下次从上游重新生成会被覆盖 —— 长期改法是在上游项目里改。

## CI

两个仓各带一份 GitHub Actions workflow（`.github/workflows/ci.yml`），push / PR 时自动跑：

- 服务端：起一个 MySQL 8.4 service → 装依赖 → `pytest tests/ -q`（244 例，连真库）；另有一个不连库的"导入与路由自检"job。
- 客户端：JDK 11 + Android SDK 30 → `./gradlew assembleDebug`，APK 作为 artifact 上传。

README 顶部的徽章地址里 `<你的账号>` 要换成实际 GitHub 账号。CI 失败时先看是"代码问题"还是"环境漂移"（runner 的 Python / Android SDK 版本），后者改 workflow 里的版本号即可。

## 测试与冒烟

- 服务端单测：`cd fastapi && python -m pytest tests/ -q`（连接**真实本地 MySQL**，不是独立测试库——写测试只用自建临时 id，别 `DROP TABLE`、别无 WHERE `DELETE`）
- 全站页面扫描：`cd fastapi && python tests/manual_page_sweep.py`（改任何页面/表单后必跑）
- 真机发送链路：`cd fastapi && python tests/manual_room_send.py --group X --private Y`
- 外网入口：`cd fastapi && python tests/manual_ext_smoke.py`

## 已知的取舍（别再"优化"回去）

- 管理后台是服务端直出 HTML，没有前端构建。可读性一般，但部署只依赖 Python。
- 客户端不藏业务逻辑：所有决策在服务端。这样客户端可以换，协议才是产品。
- 记忆与知识库刻意分两套表、两套检索：客户事实要精确命中，组织资料要语义相似。
