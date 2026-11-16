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
7. 右侧「备份与恢复」→ 下载本站备份；再选择该文件上传，数仓整体恢复，
   本机记录进入「需核对」，逐条选择重传本机内容或采用恢复库。

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

**状态区分**：列表按「全部 / 待传 / 已入仓 / 冲突 / 需核对」筛选并显示计数。

| 状态 | 含义 |
| --- | --- |
| 待传 | 本机有内容尚未成功入仓（含已发送未收到回执） |
| 已入仓 | 本地内容与最近一次回执完全一致 |
| 冲突 | 库内已被他人更正，本地与数仓两份都保留，待用户选择 |
| 需核对 | 数仓被备份恢复过，旧入仓依据已撤销；本机内容待用户对照恢复库选择 |

## 备份与恢复

误改样品后可以从「备份与恢复」页整体还原数仓，同时不丢掉采样员本机还没补传的记录。

**备份格式**：`GET /api/backup` 返回一个 JSON 文档，包含本站全部样品（样品号、站号、
时刻、pH、版号、更新时间）、站点 ID、epoch、恢复次数、样品数与 SHA-256 校验和。
下载在同一个读快照里完成。

**上传恢复**：`POST /api/restore` 接收同一 JSON 文档（请求体即文件内容）。服务端先
完整解析并校验格式版本、站点 ID、校验和、字段取值、重复样品号，全部通过后才打开
`BEGIN IMMEDIATE` 事务整体替换 `samples`。任何一步失败都返回 422 且**不改现库**；
事务中途异常/进程退出由 SQLite 回滚，不会留下半库。校验通过时 epoch + 1 并生成新的
`generation`，同时清除恢复前的幂等回执。

**恢复与上传交错**：每次 `.put` 写入都在 `BEGIN IMMEDIATE` 里先读 epoch——
恢复要么整体先完成、要么等这笔写完，每笔上传明确归属切换前或切换后。客户端请求携带
`base_epoch`（编辑所依据的 epoch）与 `generation`；若与现库不符，服务端在比对版号
**之前**就返回 `409 restore_conflict`。因此即使恢复库复用了相同样品号和版号，
恢复前的页面或未确认请求也无法凭旧依据写入。旧请求用原 `request_id` 重试同样会被
epoch 拦住，不会误报复恢复后的入仓成功。

**查询一致性**：列表、备份下载和 `/api/warehouse` 都在一个读事务中同时取样品与
epoch，只能看到完整旧库或完整恢复库。

**本机核对**：网页重新联网、下载列表或收到恢复冲突时，会读取 `/api/warehouse`
对比 epoch。凡依赖恢复前数仓的本机记录（曾入仓、有待确认提交、有冲突、或恢复库中
同号样品）进入「需核对」：保留草稿、冲突内容与未确认快照，撤销旧「已入仓」标记，
并排展示本机与恢复库字段。用户可「以本机内容重传」（按恢复库版号新建请求）、
「采用恢复库内容」或「暂不处理，保留草稿」。纯本地草稿不进入核对，只把 epoch
更新到新值后照常补传。迟到的恢复前回执只作为归档记录，不会改变核对结果或重新
点亮「已入仓」。

**兼容旧本机记录**：IndexedDB 中缺少 epoch 字段的记录按 0 处理；只要数仓从未恢复过
（`restore_count == 0`）服务端就接受它们，升级本身不会把已有记录拖进核对。数仓首次
恢复后才要求显式 epoch。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/warehouse` | 样品列表 + 数仓 epoch/恢复信息（同一快照） |
| GET | `/api/samples` | 数仓样品列表（含 `warehouse` 元信息） |
| GET | `/api/samples/{sample_no}` | 单个样品 |
| PUT | `/api/samples/{sample_no}` | 新建/更新，body：`station_no`、`sampled_at`、`ph`、`base_version`、`request_id`、`base_epoch`、`generation` |
| GET | `/api/backup` | 下载本站备份（JSON 附件） |
| POST | `/api/restore` | 上传本站备份并整体恢复；非法备份 422 且不改现库 |

旧客户端不传 `base_epoch`/`generation` 时按 0 / 空处理，只要数仓从未恢复过就能继续
工作。`409` 响应用 `error` 区分 `conflict`（版号冲突）与 `restore_conflict`（依据的
epoch 已失效）。交互式文档：`/api/docs`。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/test_api.py                      # 接口、并发、幂等、校验
pytest tests/test_backup.py                   # 备份格式、原子恢复、epoch 隔离、崩溃回滚

# 端到端测试需要一个运行中的实例
DATABASE_PATH=/tmp/e2e.db uvicorn app.main:app --port 8077 &
playwright install chromium
E2E_BASE_URL=http://127.0.0.1:8077 pytest tests/test_e2e.py   # 浏览器端到端
```

`tests/test_e2e.py` 覆盖：断网录入→关页重开→联网补传；离线队列重开后自动重试；
丢失回执后刷新重试不重复写入；发送后继续编辑不被旧回执误标；409 冲突两份内容展示、
断网重开后仍在、选择后重提或采用数仓；本地新建撞上数仓同号样品；下载数仓样品补传；
下载不覆盖本机未传修改；pH 越界表单拦截；备份下载、上传恢复后本机进入核对、非法备份
不改现库；离线草稿跨恢复保留；恢复前页面/旧请求被 epoch 挡住；迟到旧回执不改变核对
结果；核对状态断网重开仍在。

## 目录

```
app/main.py          FastAPI 路由与静态资源
app/store.py         版本控制 + 幂等写入 + epoch 隔离 + 原子恢复（SQLite 事务）
app/backup.py        备份文档的生成、校验与校验和
app/models.py        字段校验
app/db.py            schema、meta、旧库迁移与连接
app/static/          前端（index.html / app.js / app.css / sw.js / manifest）
tests/               test_api.py（接口） test_backup.py（备份恢复） test_e2e.py（Playwright）
Dockerfile           python:3.12-slim，非 root，健康检查
docker-compose.yml   单服务 + 命名卷
```

可选环境变量 `SITE_ID`（默认 `river-site`）写入备份文档；不同站点的备份会被拒绝恢复。

## 已知限制 / 后续工作

- 无用户认证与鉴权：任何能访问该 URL 的人都能写入或上传备份恢复数仓；上生产前需接入
  登录与按站/人的权限（恢复属于高权限操作）。
- 单实例 SQLite（WAL + `BEGIN IMMEDIATE`）适合小队使用；多实例部署需换成 PostgreSQL，
  用行锁/唯一约束实现同样的版本比对、epoch 与幂等语义。备份文件格式与存储实现无关。
- 恢复会整体替换 `samples`，不保留恢复前的服务端快照；误传“旧备份”后需再上传正确的
  备份来还原（epoch 会继续递增，客户端会再次进入核对）。
- 恢复是“整库替换”，没有逐样品合并；细粒度合并可在核对页后续补充。
- 前端冲突目前是字段级展示，没有逐字段合并；多人同时改同一站点的细粒度合并可后续补充。
- 冲突记录与待传记录永久保留在本机，尚无清理/归档入口。
- Service Worker 更新采用 `skipWaiting`，发版后旧页面会在下次导航时切换版本。
