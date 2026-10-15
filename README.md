# 掌心窗公开版 v0.3.7.2

## 存储层阶段 1.5-E-C（本地实现，尚未连接生产 PostgreSQL）

生产切换前代码预检：Render Blueprint 的 Python 构建现按 `server/requirements.txt` 安装依赖。PostgreSQL 模式在服务启动后由后台线程执行只读 schema 预检，不自动迁移；未通过时主动消息入口返回 503，普通命令和 `/health` 继续可用。管理 token 可访问 `GET /api/proactive/readiness`，结果只含 `ready`、`status` 与安全错误类别；修复数据库或执行独立 migration 后可再次调用该接口刷新状态。连接和 SQL 等待均有超时。进程重启后无法从内存识别的回报在后台限量查询主动消息记录，不阻塞普通命令回报。

上线前仍须独立运行 migration、核对线上旧 SQLite 数据是否需要保留，并配置生产 `DATABASE_URL` 与各独立 token。当前没有自动导入 SQLite 数据；真实主动发送默认关闭。本地代码改动不会修改线上 Render 配置。

SQLite 开发库升级到 schema v4，新增 `proactive_policy` 和 `dispatch_attempts`，保留原有三张表与旧库自动升级。已保存的设备策略会覆盖调用方传入的临时策略；全局 `LINJIAN_PROACTIVE_SEND_ENABLED` 仍默认关闭。每次手机领取都会留下独立派发记录，重试不再丢掉旧 `command_id` 的历史关联；旧尝试的迟到回报不能覆盖正在进行的新尝试。

`server/postgres_proactive_store.py` 已实现 PostgreSQL 读写，构造时不连接数据库。`LINJIAN_PROACTIVE_STORE` 默认为 `sqlite`；设为 `postgres` 时必须提供 `DATABASE_URL`，缺失会启动失败，连接故障不会回退 SQLite。普通命令轮询和设备回报在主动消息存储故障时继续处理；主动 tick 返回 `503 proactive_store_unavailable`。真实主动发送仍由 `LINJIAN_PROACTIVE_SEND_ENABLED` 控制，默认关闭。

PostgreSQL schema 不会随服务启动自动创建。准备好**单独授权的数据库**后，在服务启动前显式运行 `python server/postgres_migrate.py`，它读取 `DATABASE_URL`，按版本执行 `server/migrations/postgres/*.sql`，并在同一事务写入 `schema_migrations`。失败的版本回滚，已应用版本不重复执行。本地一次性 PostgreSQL 测试容器已验证迁移、行为对照和就绪预检；生产数据库与线上 SQLite 数据仍未核查。当前代码没有自动搬迁 SQLite 数据，也没有创建生产数据库或部署。

## 手动事项事件源（阶段 1.5-C，本地开发）

服务端现可保存一次性的用户明确事项，并在受保护的 tick 中扫描到期事项：`manual_events → trigger_candidates → proactive_messages`。事项和候选写入 `LINJIAN_DATA_DIR/proactive.sqlite3`，当前 schema 版本为 4。时间使用带时区的 ISO 8601；后端统一转为 UTC。未到期不生成候选，过期不补发；重复 tick 使用设备、事项 ID 和修订号去重。修改或取消事项会取消尚未发送的旧候选，并使旧的 `queued` 消息失效；已被手机领取的通知无法撤回。

管理接口使用单独的 `LINJIAN_PROACTIVE_ADMIN_TOKEN` 请求头 `X-Proactive-Admin-Token`，不接受设备或调度 token。`POST /api/proactive/events` 创建事项，请求示例：

```json
{"device_id":"android-phone","title":"考试","message":"记得带准考证","due_at":"2026-12-01T08:00:00+08:00","expires_at":"2026-12-01T09:00:00+08:00"}
```

`POST /api/proactive/events/{id}` 使用 `{"action":"update","changes":{"message":"新的提醒内容"}}` 修改，或 `{"action":"cancel"}` 取消。修改会增加修订号。两个接口都不会直接发送通知。调度入口仍为 `POST /api/proactive/tick`，使用 `X-Scheduler-Token`；不传 `candidates` 或传空数组时也会扫描手动事项。默认 `simulate: true`：扫描会持久化候选，但不入队或发送。响应中的 `generated_candidates` 是新候选数，`source_results` 是事项候选的规则结果，`results` 是直接提交候选的旧接口结果。真实入队仍需显式 `simulate: false` 且 `LINJIAN_PROACTIVE_SEND_ENABLED=true`；默认关闭。

目前没有内置定时触发器；没有 tick 就不会按时扫描。SQLite 仍只适合本地开发及单实例试运行。测试命令：`python -m unittest discover -s server/tests -v`，测试只使用临时 SQLite 和本地 HTTP，不连接真实设备。

## 主动消息后端实验（默认关闭）

`server` 现提供第一阶段的规则调度框架。它不会自行定时运行，也不会生成候选事件；受保护的 `POST /api/proactive/tick` 接收最多 20 条候选，并按安静时段（北京时间 23:30–08:00）、每日最多 2 条、240 分钟冷却和同日主题去重给出 `send`、`defer` 或 `skip`。请求默认 `simulate: true`，仅返回判断，不写入消息记录或发送通知。

真实入队需要同时满足：服务器设置 `LINJIAN_PROACTIVE_SEND_ENABLED=true`，且请求明确传入 `simulate: false`。未启用时真实请求返回 403。候选需包含 `device_id`、`event_id`、`topic`、`message`、ISO 8601 `expires_at`，可选 `title`。例如：

```json
{"simulate":true,"candidates":[{"device_id":"android-phone","event_id":"example-1","topic":"plan","title":"掌心窗","message":"这是模拟候选","expires_at":"2026-12-31T12:00:00Z"}]}
```

调度入口必须单独配置 `LINJIAN_SCHEDULER_TOKEN` 并传 `X-Scheduler-Token`；设备和既有 MCP 接口仍使用 `LINJIAN_TOKEN` 的 `X-Auth-Token`。URL 查询参数中的 token 不再用于鉴权，HTTP 日志不记录查询参数。真实消息写入 `LINJIAN_DATA_DIR/proactive.sqlite3`，手机现有 `/api/poll` 领取通知命令，`/api/device/report` 更新为 `completed` 或 `failed`。到期的 `queued`、`deferred` 消息在调度或手机轮询时清理为 `expired`。已领取消息五分钟没有回报会标记为 `failed`，错误码为 `dispatch_outcome_unknown`，不会自动重发；迟到的匹配回报仍可修正状态。明确的临时失败最多允许两次领取，需再次提交同一候选触发重试，并遵守五分钟退避。`completed` 只代表 Android 报告执行成功，不代表用户已阅读。相同设备、事件和主题只保留一条记录；延后的候选可再次提交以重新判断。

本阶段 SQLite 仅用于开发与单实例运行；现有 JSON 数据不迁移。容器临时磁盘、多个服务实例或进程重启后的设备状态不能当作生产持久性保证。部署前须确定持久存储、外部定时触发和多实例协调方案。此阶段未接入 Notion、AI 或 ChatGPT OAuth。

本地验证：在仓库目录运行 `python -m unittest discover -s server/tests -v`。测试使用本地临时 SQLite 和 HTTP 服务，不向真实手机发送通知。

## v0.3.7.2 快速修复

- 补充 `server/Dockerfile`、`.dockerignore` 与 `requirements.txt`，避免 Railway 在 server 服务构建阶段无法识别 Python 项目。Railway server 服务现在推荐：Root Directory=`server`，Build Command 留空，Start Command 留空，由 Dockerfile 启动 `python linjian_server.py`。

- 修复网页端 MCP 管理器连接公开版 MCP 时的跨域响应头问题。
- 设置页「连接设置」会自动保存服务器地址、Token、设备 ID 与轮询间隔，杀掉后台再打开也会保留。

## v0.3.7.2 日记与守护日历更新

- 守护日历事件补充稳定 ID，支持在日期详情卡中编辑、确认删除，并兼容没有 ID 的旧数据。
- MCP 完善 `list_guardian_days`、`add_guardian_day`、`update_guardian_day`、`delete_guardian_day`。
- 陪伴页新增本机保存的“TA 的日记”：纸质封面与纸页、左侧日期抽屉、关键词/日期搜索、封面修改和导入导出备份。
- 日记正文默认仅显示四行摘要；点击对应纸页展开全文，再次点击即可收起，切换纸页时会自动折叠上一篇。
- 日记封面增加书脊、纸页厚度、书签和立体阴影，书名信息直接居中落在封面上并按屏幕高度调整视觉位置；日记与守护日历二级页改用精简顶部，日历主体适当下移。
- 日记正文纸张改为浅粉米白渐变，使用淡灰紫横线、粉色页边线和低对比纸纤维，心情以柔和印章显示。
- “更多”菜单支持手动添加日记；MCP 支持创建、读取、搜索、更新和删除日记本及日记。
- 固定公开版签名保持不变，可继续覆盖安装旧版本。

## v0.3.6.6 稳定性修复

- 将默认轮询间隔从旧版的 1.5 秒调整为 3 秒，并对过低旧配置自动回到 3 秒，减少多人公开使用时的请求压力。
- 生活状态上传改为 10 秒限频，不再每轮都向后端上报。
- 无障碍服务改为兜底轮询：前台服务运行时不重复请求 `/api/poll`，降低系统后台压力，也减少触发 429 的概率。
- MCP 遇到 429 会返回清晰限流提示；手机端遇到 429 会短暂退避后重试。
- 无障碍异常提示补充“允许受限设置、关闭电池优化、允许后台运行”等排查步骤。


## v0.3.6.6 修复重点

- 修复 MCP 状态读取容易超过 20 秒的问题：`get_phone_state` 改为快速读取服务器缓存，不再被活动日志记录或长轮询拖慢。
- 缩短 MCP 到后端、命令排队和命令状态查询的默认等待时间，避免 Render 冷启动/网络波动时整条工具链卡死。
- `open_app` / 门禁类工具增加空参数拦截：缺少 App 名称或包名时直接返回明确错误，不再下发到手机端形成 `package_empty`。
- 增加常用 App 名称到包名的 MCP 侧兜底映射，例如小红书、抖音、微信、QQ、QQ音乐。
- 保留 v0.3.6.3 的守护日历保存修复与应用门禁旧版工具名兼容。
- 清理公开包里的 `server/.env` 与 `__pycache__`，只保留 `.env.example`。
- 固定签名保持不变，可覆盖安装上一版公开版。


[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/linzhi-524/linjian-peek-public)

> Railway 一键部署按钮需要先在 Railway 控制台生成 Template Code。生成后，把 `docs/railway-one-click.md` 里的 `YOUR_TEMPLATE_CODE` 替换掉，再把按钮复制到这里即可。

掌心窗公开版是一个可自部署的 Android 陪伴窗：手机端负责展示、截图/读屏、通知、日历、门禁与息屏等本机能力；同步后端负责保存状态和下发指令；MCP 服务负责把这些能力暴露给 AI 客户端使用。

公开版对齐私人版的粉白卡片 UI，但不复制私人绑定内容。它适合作为公开模板，让用户自己填写名字、目标 App 和服务器配置。

## 本版内容

- **今日页**：今日窗语、今日专注、天气、下一件事、手机电量、今日轨迹。
- **陪伴页**：陪伴对象卡片、最近一句话、陪伴天数、下个纪念日、行动记录、归电入口，以及本机保存的“TA 的日记”。
- **守护页**：守护日历、目标 App 设置、应用门禁、屏幕休息、提醒、天气、息屏。
- **设置页**：服务器连接、Token、设备 ID、用户名称、陪伴对象名称、目标 App、权限、主动提醒、周期、主题、调试、版本更新、许可。
- **MCP**：截图、读屏、点击、输入、通知、天气、日历、归电、门禁、屏幕休息、提醒、到访记录、关心策略等通用工具。

公开版只保留适合公开发布和自部署的通用能力，不包含私人绑定内容、私人 Token、私人服务地址、固定人物关系或不可公开的专属接口。

## 个性化配置

在 Android 设置页填写：

- `userName`：用户自己的名字，留空时默认“宝宝”。
- `companionName`：AI 或陪伴对象名字，留空时默认“陪伴者”。
- `targetApps`：目标 App 列表，每行一个，格式为 `名称|Android包名`，例如 `ChatGPT|com.openai.chatgpt`。

姓名会用于主要界面文案、归电页、提醒、门禁和行动记录。目标 App 会用于归电目标、守护统计、门禁/屏幕休息、回家模式和 MCP 打开 App。

## 目录结构

```text
android/      Android 客户端和 GitHub Actions 构建脚本
server/       Python 同步后端，零第三方依赖
mcp/          Node.js MCP 服务
docs/         安装、版本和 MCP 工具说明
render.yaml   Render Blueprint 一键部署配置
update.json   版本更新信息
```

## 构建 APK

### GitHub Actions 构建

1. 将源码包解压并覆盖到公开仓库根目录，确保 `.github`、`android`、`server`、`mcp` 位于根目录。
2. 打开 GitHub 仓库 → **Actions** → **Build Android Public APK** → **Run workflow**。
3. 构建成功后下载 `zhangxinchuang-public-v0.3.7.2-apk` artifact。

构建产物为：

```text
android/Zhangxinchuang-public-v0.3.7.2.apk
```

版本名 `0.3.7.2`，版本码 `30702`。

### 固定签名

公开版已经使用固定签名：

```text
android/signing/zhangxinchuang-public-release.p12
```

这份证书与私人版完全分离。后续公开版升级必须保留这份证书，否则已安装用户无法直接覆盖升级。不要把私人版签名混入公开版。

## Render 一键部署

点击 README 顶部的 **Deploy to Render**，或在 Render 中使用本仓库的 `render.yaml` Blueprint。

Blueprint 会创建两个 Web Service：

- `zhangxinchuang-server`：手机端连接的同步后端。
- `zhangxinchuang-mcp`：AI/MCP 客户端连接的 MCP 服务。

Blueprint 会自动生成并共享：

- `LINJIAN_TOKEN`：手机端、后端和 MCP 共同使用的访问令牌。
- `LINJIAN_DEFAULT_DEVICE`：默认设备 ID，默认 `android-phone`。
- `LINJIAN_URL`：MCP 自动引用 `zhangxinchuang-server` 的公网 HTTPS 地址，不需要手动填写。

部署完成后：

1. 打开 `zhangxinchuang-server` 的公网地址，访问 `/health`，看到 `ok: true` 即后端在线。
2. 打开 `zhangxinchuang-mcp` 的公网地址，访问 `/health`，看到 `has_url: true`、`has_token: true` 即 MCP 配置完成。
3. 在 Android 设置页填写：
   - 服务器地址：`zhangxinchuang-server` 的公网地址，不要多余斜杠。
   - Token：Render 自动生成的同一个 `LINJIAN_TOKEN`。
   - 设备 ID：建议与 `LINJIAN_DEFAULT_DEVICE` 一致，例如 `android-phone`。
4. 在 AI/MCP 客户端里填写 MCP 地址：
   - Streamable HTTP：`https://你的-mcp-域名/mcp`
   - SSE：`https://你的-mcp-域名/sse`

如果你是从旧版 0.3.6.3 更新上来，**直接重新部署 MCP 服务即可**。新版 MCP 会兼容旧环境变量：即使 `LINJIAN_URL` 仍然是旧版自动写入的 `http://zhangxinchuang-server-xxxx:10000` 内网地址，也会自动兜底转换为 `https://zhangxinchuang-server-xxxx.onrender.com` 公网地址再连接。

如果你重新同步/刷新 Blueprint，新版会自动把 `LINJIAN_URL` 改为引用 server 的公网 `RENDER_EXTERNAL_URL`；如果你只点 **Redeploy**，也可以依靠新版 MCP 的兜底逻辑修复，不需要用户手动复制 URL。

验证方式：打开 `zhangxinchuang-mcp` 的 `/health`，如果看到 `fallback_linjian_urls` 里出现 `https://...onrender.com`，说明旧内网地址兼容逻辑已经生效。

## Railway 一键部署

Railway 的一键部署依赖 **Railway Template**。本仓库已经整理好 server 与 MCP 的双服务配置说明；真正的一键按钮需要你先在 Railway 控制台把双服务项目生成为模板，然后得到 Template Code。

模板生成后，把下面链接中的 `YOUR_TEMPLATE_CODE` 替换成真实模板码，再放到 README 顶部即可：

```markdown
[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new/template/YOUR_TEMPLATE_CODE?utm_medium=integration&utm_source=button&utm_campaign=zhangxinchuang)
```

模板内建议使用两个服务：

### server 服务

```text
Service Name：server
Root Directory：server
Build Command：留空
Start Command：留空
Dockerfile Path：Dockerfile
Healthcheck Path：/health
Public Networking：开启 HTTP 域名
```

环境变量：

```env
LINJIAN_TOKEN=${{ shared.LINJIAN_TOKEN }}
LINJIAN_HOST=0.0.0.0
LINJIAN_KEEP=3
LINJIAN_DEFAULT_DEVICE=${{ shared.LINJIAN_DEFAULT_DEVICE }}
```

### mcp 服务

```text
Service Name：mcp
Root Directory：mcp
Build Command：留空
Start Command：pnpm start
Healthcheck Path：/health
Public Networking：开启 HTTP 域名
```

环境变量：

```env
LINJIAN_URL=https://${{ server.RAILWAY_PUBLIC_DOMAIN }}
LINJIAN_TOKEN=${{ shared.LINJIAN_TOKEN }}
LINJIAN_DEFAULT_DEVICE=${{ shared.LINJIAN_DEFAULT_DEVICE }}
```

### Shared Variables

在 Railway Template 中设置共享变量：

```env
LINJIAN_TOKEN=${{ secret(48) }}
LINJIAN_DEFAULT_DEVICE=android-phone
```

部署完成后：

1. 访问 `https://你的-server-域名/health`，确认后端在线。
2. 访问 `https://你的-mcp-域名/health`，确认 MCP 已连上 server。
3. Android 设置页填写 server 公网地址、同一个 Token 和设备 ID。
4. AI/MCP 客户端填写 `https://你的-mcp-域名/mcp` 或 `https://你的-mcp-域名/sse`。

更完整的 Railway 模板按钮配置见 [docs/railway-one-click.md](docs/railway-one-click.md)。手动双服务部署流程见 [docs/setup.md](docs/setup.md)。

## Railway 手动双服务部署

如果暂时没有 Railway Template Code，也可以手动部署。Railway 建议把后端和 MCP 分成两个服务，它们可以来自同一个 GitHub 仓库，但 Root Directory、启动命令和环境变量不同。

### 第一步：准备 Token

先生成一个长随机 Token：

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
```

后面的 server、mcp、Android 设置页都使用同一个 Token。

### 第二步：部署 server 服务

在 Railway 新建一个 Project，然后从 GitHub 仓库创建第一个服务：

```text
服务名：server
Root Directory：server
Build Command：留空
Start Command：留空
Dockerfile Path：Dockerfile
```

环境变量：

```env
LINJIAN_TOKEN=第一步生成的长随机token
LINJIAN_HOST=0.0.0.0
LINJIAN_KEEP=3
LINJIAN_DEFAULT_DEVICE=android-phone
```

注意：Railway 会自动提供 `PORT`，不要强行写死 `LINJIAN_PORT`。后端代码会优先读取 Railway 的 `PORT`。server 目录已经内置 `Dockerfile`，Root Directory 设为 `server` 后，Railway 不需要再猜 Python 构建方式。

部署完成后，给 server 服务生成公网域名，访问：

```text
https://你的-server-域名/health
```

看到 `ok: true` 就说明 server 可用。

### 第三步：部署 MCP 服务

在同一个 Railway Project 中，从同一 GitHub 仓库再创建第二个服务：

```text
服务名：mcp
Root Directory：mcp
Build Command：留空
Start Command：pnpm start
```

环境变量：

```env
LINJIAN_URL=https://你的-server-域名
LINJIAN_TOKEN=第一步生成的同一个长随机token
LINJIAN_DEFAULT_DEVICE=android-phone
```

`LINJIAN_URL` 必须填写 server 的公网地址，不要填 MCP 自己的地址，也不要在末尾加 `/`。

部署完成后，给 MCP 服务生成公网域名，访问：

```text
https://你的-mcp-域名/health
```

看到 `ok: true`、`has_url: true`、`has_token: true` 就说明 MCP 可用。

AI/MCP 客户端连接：

```text
https://你的-mcp-域名/mcp
```

或：

```text
https://你的-mcp-域名/sse
```

### 第四步：连接 Android

Android 设置页填写：

```text
服务器地址：https://你的-server-域名
Token：第一步生成的同一个 token
设备 ID：android-phone
```

然后打开无障碍服务、通知权限、使用情况访问权限，回到 App 点击启动。用“测试截图上传”或 MCP 的 `linjian_status` 检查是否连通。

如果开启无障碍后回到掌心窗仍显示“已关闭/待开启”，请先安装本版或更新后的 APK；本版已修复无障碍服务声明与状态检测。若系统提示“受限设置/出于安全考虑不可用”，请到系统设置 → 应用 → 掌心窗 → 右上角菜单 → 允许受限设置，然后再回无障碍里开启“掌心窗服务”。

## 局域网部署教程

局域网部署适合只在自己电脑和自己手机之间使用。电脑和手机需要连接同一个 Wi-Fi。

### 启动 server

```bash
cd server
cp .env.example .env
```

修改 `server/.env`：

```env
LINJIAN_TOKEN=你的长随机token
LINJIAN_HOST=0.0.0.0
LINJIAN_PORT=8513
LINJIAN_KEEP=3
LINJIAN_DEFAULT_DEVICE=android-phone
```

启动：

```bash
python3 linjian_server.py
```

查看电脑局域网 IP，例如 `192.168.1.23`。手机端服务器地址填写：

```text
http://192.168.1.23:8513
```

如果手机连不上，优先检查：电脑防火墙、手机和电脑是否同一 Wi-Fi、地址是否多写斜杠、Token 是否一致。

### 启动本地 MCP

```bash
cd mcp
npm install
LINJIAN_URL=http://192.168.1.23:8513 LINJIAN_TOKEN=你的长随机token npm start
```

默认 MCP 监听：

```text
http://127.0.0.1:8787/mcp
http://127.0.0.1:8787/sse
```

如果 AI/MCP 客户端和 MCP 服务不在同一台电脑，需要把 `127.0.0.1` 换成 MCP 所在电脑的局域网 IP，并确认防火墙放行端口 `8787`。

Android 允许连接自建局域网 HTTP 地址；公网部署仍建议使用 HTTPS。

## MCP 工具概览

MCP 详细工具说明见 [docs/mcp.md](docs/mcp.md)。常用工具分组如下：

- **状态与截图**：`linjian_status`、`peek_screen`、`latest_screen`、`get_life_state`、`get_phone_state`、`get_screen_nodes`。
- **点击与输入**：`tap_text`、`input_text`、`send_phone_command`、`run_sequence`、`run_preset`。
- **App 控制**：`open_app`、`phone_home`、`phone_back`、`phone_recents`、`phone_screen_off`、`list_known_apps`、`save_known_app`。
- **通知与提醒**：`send_notification`、`set_alarm`、`get_weather_state`、`send_weather_notification`。
- **日历与窗语**：`get_window_whisper`、`set_window_whisper`、`list_guardian_days`、`add_guardian_day`、`update_guardian_day`、`delete_guardian_day`。
- **TA 的日记**：创建/重命名日记本，写入、读取、搜索、更新和删除本机日记；正文不默认上传云端。
- **归电与关心**：`get_guidian_state`、`set_guidian_config`、`trigger_guidian`、`mark_guidian_returned`、`active_care_check`、`care_action`。
- **门禁/屏幕休息**：`screen_break_app`、`temporary_screen_break_release`、`end_screen_break`、`extend_screen_break`、`get_screen_break_state`、`list_screen_break_apps`、`add_screen_break_app`、`set_screen_break_passphrase`。
- **到访与活动**：`record_visit`、`get_last_visit`、`get_visit_history`、`get_visit_stats`、`get_activity_events`、`add_activity_event`、`get_companion_actions`。

截图、读屏、自动点击、输入、门禁、息屏和自动评论都属于敏感能力，只应在本人设备、本人服务和明确授权的 MCP 客户端中使用。

## 安全边界

- 不要公开 `LINJIAN_TOKEN`。
- 不要把 MCP 服务连接到不可信客户端。
- 不要用掌心窗管理别人的设备。
- 自动发送评论、点击按钮、读屏、截图、门禁、息屏等动作建议默认手动确认。
- 公开版不应写死私人姓名、私人关系、私人服务地址或私人接口。

许可条款见 [LICENSE](LICENSE)。

> v0.3.6.6 补充：无障碍状态会在从系统设置返回后延迟复查多次，并兼容不同系统写入无障碍组件名的格式差异；如果侧载 APK 被系统拦截，App 会提示去“应用信息 → 允许受限设置”，再回无障碍开启“掌心窗服务”。
