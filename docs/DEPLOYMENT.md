# 启动与部署

## 本地

见 README 的 `uv sync`、前端构建、迁移、`scripts/manage.py start`。API 默认仅监听 127.0.0.1:8010，并校验 Host、写请求 Origin；不需要把密钥放入前端。管理脚本使用独立进程会话，浏览器关闭后仍运行。运行日志与 PID 文件在 `data/`。

生产不要开启 `--reload`。本版按单市场 worker、单研究 worker部署；跨主机多副本的作业恢复和 leader 选举尚未验收，不应横向复制 worker。停止服务不是清仓操作；先在界面检查未完成订单及本地保护状态。系统睡眠或主机关机都会停止本地保护监控。

## PostgreSQL 与常驻容器

```bash
cp .env.example .env
# 设置 APP_ACCESS_TOKEN、POSTGRES_PASSWORD；公开域名还须配置 ALLOWED_HOSTS/ALLOWED_ORIGINS。
docker compose up --build -d
```

Compose 包含 PostgreSQL16、一次性迁移、API、市场 worker、研究 worker。API 端口仅绑定宿主机回环，公开访问须增加 HTTPS 反向代理。真实行情账户在 PostgreSQL，演示 SQLite 放在独立持久卷文件。模型、金额精度与任务队列兼容 PostgreSQL；本次未运行 Docker/PostgreSQL 实例，不能把配置文件视为部署验证。

`APP_ACCESS_TOKEN` 是单用户应用访问口令，浏览器通过 `/api/session` 登录，HttpOnly SameSite cookie 保存会话。API 重启令会话失效。公共部署前增加网关限流、TLS、备份、进程告警和受控密钥存储。不要公开未受保护的 API、Swagger 或数据库端口。

主机迁移前备份数据库和 `data/research-seals.json`。SQLite 运行时采用 WAL，应通过 SQLite backup API 创建一致快照；不要仅复制正在写入的主 DB 文件。PostgreSQL 使用 pg_dump/恢复演练。

## Vercel 前端 + 常驻后端

Vercel 可以承载 `frontend/` 的 Vite 静态构建。巡检和研究是长期进程，部署方案采用单独常驻后端，不把循环放入有执行时限的请求函数。函数限制参考 [Vercel Functions Limits](https://vercel.com/docs/functions/limitations)。

1. 把后端 API、worker、research 和 PostgreSQL 部署到持续运行的容器/VM。
2. Vercel 项目 Root Directory=`frontend`，Build=`npm run build`，Output=`dist`。
3. 在前端项目配置同源 `/api/:path*` rewrite 指向后端 HTTPS API；`vercel.json` 示例见下。后端仅接受受控网关访问，设置 Host 和 Origin 允许列表。
4. 保持 Cookie 同源，通过平台 HTTPS 登录；凭证永不放入静态构建或 `VITE_*` 环境变量。
5. 验证 SSE 代理和断线恢复。前端另有 15 秒查询兜底；SSE 不承担交易调度。

```json
{
  "rewrites": [
    {"source": "/api/:path*", "destination": "https://YOUR-BACKEND/api/:path*"},
    {"source": "/((?!assets/).*)", "destination": "/index.html"}
  ]
}
```

示例中的后端域名必须替换；未创建 Vercel 项目或上传任何账户数据。本次验收使用本地启动地址，云部署需要用户自己的基础设施与授权。

## 监控

`/api/state` 展示市场 worker 心跳、最近成功时间和剩余挂单。超过 75 秒未更新心跳显示 offline。真实数据每轮按 XNYS 会话和数据新鲜度决定能否产生指令；预/盘后不会自动交易。demo_ready 表示等待显式回放，不是实时行情巡检成功。

`/api/jobs/{id}` 查询后台任务，queued/running/completed/failed/interrupted 必须区分。回测有独立进程，任务中断后标记 interrupted，不会重放交易副作用。
