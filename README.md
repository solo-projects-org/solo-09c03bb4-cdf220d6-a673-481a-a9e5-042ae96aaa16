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
`APP_PORT=8080 docker compose up --build` 覆盖。站标识用 `SITE_ID=...` 覆盖；备份
只能在同一 `SITE_ID` 的实例间恢复。

演示步骤（浏览器 DevTools → Application → Service Workers 可看到缓存结果）：

1. 联网打开 `http://localhost:8000`，等右上角出现「离线缓存就绪」。
2. DevTools → Network 勾选 **Offline**，刷新页面。页面仍能加载，顶部显示「离线」。
3. 填写样品号（如 `RIVER-2026-0007`）、站号、UTC 采样时刻、pH（0–14），自动保存到本机。
4. 关闭标签页再打开（保持 Offline），记录与修改仍在。
5. 取消 Offline，点「上传到数仓」→ 状态变「已入仓」。
6. 点「下载列表」可从数仓载入已有样品，修改后补传。
7. 右下「备份恢复」→「下载本站备份」保存 JSON；改动若干样品后「上传备份并恢复」，
   确认后整库回到备份内容。本机旧「已入仓」记录会转为「待核对」，在「恢复检测」
   面板比对后选择保留本机并补传或采用恢复库。

### 不用 Docker 直接跑

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_PATH=./data/samples.db SITE_ID=river-sampling-site \
  uvicorn app.main:app --reload --port 8000
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
| 待核对 | 数仓恢复后，本机旧依据失效；保留内容，等待与恢复库比对后补传 |

## 备份恢复

右侧「备份恢复」面板：

- **下载本站备份**：导出全部样品（样品号、站号、采样时刻、pH、版号、更新时间）
  与本代数仓身份，带 SHA-256 校验和，下载为 JSON 文件。
- **上传备份并恢复**：只接受本站、同格式版本（`format_version = 1`）、校验和正确、
  字段全部合法的备份。上传会先展示摘要，确认后才整库替换。

数仓用「代」（generation）与随机 `epoch` 标识数据代际。每次恢复：

1. 在单个 `BEGIN IMMEDIATE` 事务内删旧样品、写备份样品、提升代际并换新 `epoch`。
   读者只可能看到完整的旧库或完整的新库；写入失败或进程退出会整笔回滚，不留半库。
2. 清空幂等表。恢复前请求的 `request_id` 不会在恢复后重放成功；即使样品号与版号
   和恢复库完全相同，也不会凭旧依据写入。
3. 客户端请求携带 `epoch`；与当前代不符返回 `409 epoch_changed`，不写入。
   恢复后未携带 `epoch` 的写入返回 `409 epoch_required`（不猜测其归属）；首次恢复
   之前为兼容旧客户端仍可省略。上传与恢复交错时，每笔上传要么明确属于切换前
   （被拒或写入旧代后被整库替换），要么属于切换后（按恢复库判断），不存在含混归属。
   每次写入响应都带 `X-Warehouse-Epoch`（实际提交的代）与
   `X-Warehouse-Current-Epoch`（响应时的当代），两者不同即说明恢复与请求发生了交错。

前端行为：

- 联网重连或下载数仓列表时读取 `/api/warehouse` 的 `epoch`。发现变化即撤销所有旧
  「已入仓」标记（`last_receipt`、旧 `base_version`），但**保留**本机草稿、冲突内容与
  未确认提交快照。
- 展示「本机内容 / 恢复库内容」逐条对比，用户选择「保留本机并补传」或「采用恢复库
  版本」；补传使用恢复库的版号与当前 `epoch`。
- 恢复前发出、恢复后才到达的回执因携带旧 `epoch` 而不会用于确认；核对结果不被迟到
  旧回执改变。响应头同时给出提交代与当代，客户端据此丢弃交错回执。
- 旧版本客户端记录没有 `epoch` 字段：若数仓从未恢复（第 1 代）则按当前代认领，原采样
  与补传功能不变；若已恢复过，则转为「待核对」，旧版号不再能直接写入。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/warehouse` | 数仓身份：`epoch`、`generation`、恢复记录、样品数 |
| GET | `/api/samples` | 数仓样品列表及一致性快照的代际 |
| GET | `/api/samples/{sample_no}` | 单个样品 |
| PUT | `/api/samples/{sample_no}` | 新建/更新，body：`station_no`、`sampled_at`、`ph`、`base_version`、`request_id`，可选 `epoch` |
| GET | `/api/backup` | 下载本站样品备份（JSON 附件） |
| POST | `/api/backup/restore` | 上传本站同版备份整库恢复；非法备份返回 422 且不改库 |

交互式文档：`/api/docs`。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/test_api.py tests/test_backup.py   # 接口、并发、幂等、校验、备份恢复

# 端到端测试需要一个运行中的实例
DATABASE_PATH=/tmp/e2e.db uvicorn app.main:app --port 8077 &
playwright install chromium
E2E_BASE_URL=http://127.0.0.1:8077 pytest tests/test_e2e.py   # 浏览器端到端
```

`tests/test_e2e.py` 覆盖：断网录入→关页重开→联网补传；离线队列重开后自动重试；
丢失回执后刷新重试不重复写入；发送后继续编辑不被旧回执误标；409 冲突两份内容展示、
断网重开后仍在、选择后重提或采用数仓；本地新建撞上数仓同号样品；下载数仓样品补传；
下载不覆盖本机未传修改；pH 越界表单拦截；备份下载/非法备份拒绝/恢复后撤销旧入仓
标记并核对补传/迟到旧回执不改核对结果。

`tests/test_backup.py` 覆盖：备份内容与校验和、各类非法备份不改库、整库替换与代际
提升、同站/异站校验、恢复后旧 `epoch` 写入被拒、旧 `request_id` 不重放成功、旧库
平滑迁移、并发读只看到完整旧库或完整新库。

## 目录

```
app/main.py          FastAPI 路由与静态资源
app/store.py         版本控制 + 幂等写入（SQLite 事务）、备份导出与整库恢复
app/backup.py        备份文件校验（格式、站、校验和、字段）
app/models.py        字段校验
app/db.py            schema、代际/epoch 元数据与连接
app/static/          前端（index.html / app.js / app.css / sw.js / manifest）
tests/               test_api.py（接口） test_backup.py（备份恢复） test_e2e.py（Playwright）
Dockerfile           python:3.12-slim，非 root，健康检查
docker-compose.yml   单服务 + 命名卷
```

## 已知限制 / 后续工作

- 无用户认证与鉴权：任何能访问该 URL 的人都能写入、下载备份或恢复整库；上生产前需
  接入登录与按站/人的权限，并限制备份恢复为管理员操作。
- 单实例 SQLite（WAL + `BEGIN IMMEDIATE`）适合小队使用；多实例部署需换成 PostgreSQL，
  用行锁/唯一约束实现同样的版本比对、幂等与整库恢复语义。
- 备份是逻辑备份（JSON，含样品与版号），不含幂等表；这是有意为之——恢复后的旧回执
  一律失效。备份文件未加密，含样品数据，需按现场数据规范保管。
- 前端冲突目前是字段级展示，没有逐字段合并；多人同时改同一站点的细粒度合并可后续补充。
- 恢复后本机「待核对」记录需用户逐条或批量确认；没有自动合并策略，避免替用户做数据
  取舍决定。
- Service Worker 更新采用 `skipWaiting`，发版后旧页面会在下次导航时切换版本；恢复
  检测依赖 `/api/warehouse` 或写入响应头，离线期间发生的多次恢复会在联网后一次提示。
