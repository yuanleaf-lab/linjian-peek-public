# 主动消息生产部署清单（Neon Free）

本清单只用于部署准备。创建云资源、修改 Render 环境变量、运行生产 migration、部署和开启真实发送都需要分别确认。

## 数据策略

- 生产 PostgreSQL 按空库初始化。
- Git 历史确认远程版本从未包含主动消息数据库及写入逻辑，因此不迁移旧版主动消息 SQLite。
- 不删除 Render 或本地已有文件和数据。
- 正式部署前再次核对 Render 当前服务的仓库、分支和 deployed commit。

## Neon 连接

Neon 提供 direct 和 pooled 两类连接串，均须使用 TLS。不要把真实连接串写进仓库、命令历史或日志。

- Migration：使用 Neon 的 direct connection string。
- Render 后端：使用 Neon 的 pooled connection string，主机名通常包含 `-pooler`。
- 连接串应保留 Neon 控制台给出的 TLS 参数，至少包含 `sslmode=require`；如果控制台同时给出 `channel_binding=require`，完整保留。
- 当前 Store 每次操作只在事务范围内打开连接，兼容 Neon PgBouncer 的 transaction pooling。
- `psycopg` 连接超时为 5 秒，服务端 statement/lock timeout 为 3 秒。

## Render 环境变量

以下变量只配置在 `zhangxinchuang-server`，不要配置到 Android 或 `zhangxinchuang-mcp`：

| 变量 | 初始值或来源 | 说明 |
| --- | --- | --- |
| `LINJIAN_PROACTIVE_STORE` | `postgres` | 选择 PostgreSQL；不会回退 SQLite。 |
| `DATABASE_URL` | Neon pooled connection string | Render secret；不得写入仓库。 |
| `LINJIAN_PROACTIVE_SEND_ENABLED` | `false` | 首次部署必须保持关闭。 |
| `LINJIAN_SCHEDULER_TOKEN` | 独立随机凭据 | 仅供未来调度器调用 tick，不得复用设备 token。 |
| `LINJIAN_PROACTIVE_ADMIN_TOKEN` | 独立随机凭据 | 仅供事项管理和 readiness，不得复用其他 token。 |

现有 `LINJIAN_TOKEN` 继续用于 Android 与普通命令通信，不修改、不复用。Blueprint 中三个敏感值使用 `sync: false`，只是声明需要人工配置，不包含真实值。

## 首次 migration

在已安装 `server/requirements.txt` 的可信本地环境中，临时把 `DATABASE_URL` 设为 Neon direct connection string，然后从仓库根目录执行：

```powershell
$env:DATABASE_URL = '<NEON_DIRECT_DATABASE_URL>'
python server/postgres_migrate.py
Remove-Item Env:DATABASE_URL
```

期望首次输出：

```text
Applied migrations: [1]
```

再次执行应输出 `Applied migrations: []`。Migration runner 不会由服务启动自动调用。

## Schema 验证

使用 direct connection string，通过 `psql` 执行只读校验：

```powershell
psql '<NEON_DIRECT_DATABASE_URL>' -c "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
psql '<NEON_DIRECT_DATABASE_URL>' -c "SELECT to_regclass(name) FROM unnest(ARRAY['manual_events','trigger_candidates','proactive_messages','proactive_policy','dispatch_attempts']) AS name;"
```

应只存在 migration 版本 `1`，五张业务表均返回非空表名。服务部署后，以管理 token 调用：

```powershell
$headers = @{ 'X-Proactive-Admin-Token' = '<ADMIN_TOKEN>' }
Invoke-RestMethod -Headers $headers -Uri 'https://<SERVER_HOST>/api/proactive/readiness'
```

期望结果为 `ready=true`、`status=ready`。接口不会返回连接串、token 或数据库异常正文。

## 部署顺序

1. 确认 Render 服务仍连接预期仓库和分支，并记录当前 deployed commit。
2. 创建 Neon Free 项目、数据库和生产分支。
3. 使用 direct URL 执行 migration，并重复执行确认幂等。
4. 完成 schema 只读校验。
5. 在 Render 后端服务配置五个主动消息变量，真实发送保持 `false`。
6. 确认 Render 服务与 Blueprint 的自动部署均关闭。
7. 提交并推送经过审查的代码；推送本身不得触发部署。
8. 手动部署后端，检查 `/health` 仍正常，再检查受保护 readiness。
9. 验证普通命令领取和回报；数据库不可用演练只影响主动消息入口。
10. 后续另行创建 Cloudflare Worker Cron。开启真实发送需要新的明确确认。

## 回滚与故障隔离

- PostgreSQL 未就绪时，主动事项、tick 和主动领取保持不可用；普通命令和 `/health` 继续服务。
- 首次部署失败时，保持 `LINJIAN_PROACTIVE_SEND_ENABLED=false`，回滚应用版本，不删除 Neon 数据。
- DATABASE_URL 配置错误时修正 secret 后重新部署或刷新 readiness，不切回 SQLite。
- 不把本地测试 URL、测试 schema 或测试记录导入 Neon。
