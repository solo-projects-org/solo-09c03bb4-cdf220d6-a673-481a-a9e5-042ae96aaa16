# 河段水质采样 · 离线优先录补系统

野外无网河段用手机/笔记本打开网页即可录入样品；回站联网后补传。后端 FastAPI +
SQLite，前端为无构建步骤的原生 HTML/CSS/JS，Service Worker 缓存应用外壳，IndexedDB
保存本机台账。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据持久化在命名卷 `sampling-data`（容器内 `/data/samples.db`）。默认端口可用
`APP_PORT=8080 docker compose up --build` 覆盖。

演示步骤（浏览器 DevTools → Application → Service Workers 可看到缓存结果）：

1. 联网打开 `http://localhost:8000`，等右上角出现「离线缓存就绪」。
2. DevTools → Network 勾选 **Offline**，刷新页面。页面仍能加载，顶部显示「离线」。
3. 填写样品号（如 `RIVER-2026-0007`）、站号、UTC 采样时刻、pH（0–14），自动保存到本机。
4. 关闭标签页再打开（保持 Offline），记录与修改仍在。
5. 取消 Offline，点「上传到数仓」→ 状态变「已入仓」。
6. 点「下载列表」可从数仓载入已有样品，修改后补传。

### 不用 Docker 直接跑

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_PATH=./data/samples.db uvicorn app.main:app --reload --port 8000
```

## 功能与设计要点

**唯一键与离线录入**：样品号即 SQLite 主键。本机台账也以样品号为主键；再次输入已有
样品号会载入原记录，不会产生第二条。pH 限 0–14（越界前后端都拒绝），采样时刻按 UTC
填写并统一存成 `YYYY-MM-DDTHH:MM:SSZ`。

**离线可用**：Service Worker 只缓存应用外壳，`/api/` 一律不缓存（避免把过期数仓内容
当真实内容）。数据落在 IndexedDB，关页/断网重开都保留。

**补传与冲突**：每次提交带 `base_version`（编辑所基于的数仓版本）。服务端在
`BEGIN IMMEDIATE` 事务里比对版本：
- 一致则写入、版本 +1；
- 不一致返回 409，响应里同时带本地提交内容与数仓当前内容，不覆盖。

前端收到 409 后展示「本地内容 / 数仓内容」两栏，用户选「保留本地内容并重提」（按数仓
最新版本重提）或「采用数仓版本」。冲突状态写入 IndexedDB，断网重开仍在。

**丢失回执 / 重复写入**：每次提交在发请求前就把不可变快照（含客户端生成的
`request_id`）写入 IndexedDB。服务端把每个 `request_id` 的首次响应原样存入
`idempotency` 表；客户端刷新或重试时用同一 `request_id` 重放，不会二次写入，也不会
因 `base_version` 已过期而误报冲突。同一 `request_id` 携带不同内容会被 422 拒绝。

**发送后继续编辑**：回执只确认「发出的那份快照」。前端用 `last_receipt.payload` 与
当前本地内容比较：一致才标「已入仓」，不一致则保持「待传」并提示「上次提交 vN 已入仓；
之后的新修改仍待上传」。

**状态区分**：列表按「全部 / 待传 / 已入仓 / 冲突」筛选并显示计数。

| 状态 | 含义 |
| --- | --- |
| 待传 | 本机有内容尚未成功入仓（含已发送未收到回执） |
| 已入仓 | 本地内容与最近一次回执完全一致 |
| 冲突 | 库内已被他人更正，本地与数仓两份都保留，待用户选择 |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/samples` | 数仓样品列表 |
| GET | `/api/samples/{sample_no}` | 单个样品 |
| PUT | `/api/samples/{sample_no}` | 新建/更新，body：`station_no`、`sampled_at`、`ph`、`base_version`、`request_id` |

交互式文档：`/api/docs`。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/test_api.py                      # 接口、并发、幂等、校验

# 端到端测试需要一个运行中的实例
DATABASE_PATH=/tmp/e2e.db uvicorn app.main:app --port 8077 &
playwright install chromium
E2E_BASE_URL=http://127.0.0.1:8077 pytest tests/test_e2e.py   # 浏览器端到端
```

`tests/test_e2e.py` 覆盖：断网录入→关页重开→联网补传；离线队列重开后自动重试；
丢失回执后刷新重试不重复写入；发送后继续编辑不被旧回执误标；409 冲突两份内容展示、
断网重开后仍在、选择后重提或采用数仓；本地新建撞上数仓同号样品；下载数仓样品补传；
下载不覆盖本机未传修改；pH 越界表单拦截。

## 目录

```
app/main.py          FastAPI 路由与静态资源
app/store.py         版本控制 + 幂等写入（SQLite 事务）
app/models.py        字段校验
app/db.py            schema 与连接
app/static/          前端（index.html / app.js / app.css / sw.js / manifest）
tests/               test_api.py（接口） test_e2e.py（Playwright）
Dockerfile           python:3.12-slim，非 root，健康检查
docker-compose.yml   单服务 + 命名卷
```

## 已知限制 / 后续工作

- 无用户认证与鉴权：任何能访问该 URL 的人都能写入；上生产前需接入登录与按站/人的权限。
- 单实例 SQLite（WAL + `BEGIN IMMEDIATE`）适合小队使用；多实例部署需换成 PostgreSQL，
  用行锁/唯一约束实现同样的版本比对与幂等语义。
- 前端冲突目前是字段级展示，没有逐字段合并；多人同时改同一站点的细粒度合并可后续补充。
- 冲突记录与待传记录永久保留在本机，尚无清理/归档入口。
- Service Worker 更新采用 `skipWaiting`，发版后旧页面会在下次导航时切换版本。
