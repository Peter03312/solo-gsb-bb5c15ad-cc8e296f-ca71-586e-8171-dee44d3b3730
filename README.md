# 设备停机归因服务（Downtime Attribution）

按**班次**对设备停机时间进行归因的 HTTP 服务：输入设备、班次（带时区、整分钟）与停机事件
（来源编号 + 连续递增版本 + 起止 + 原因），输出各班次的**保养 / 故障 / 待判**分钟数及所依据
的事件版本；支持事件幂等上报、版本链校验、迟到更正重算与结算修订查询；上报下一版本前可经
**预演-确认**两步提交：先只读预演候选版本对已结算班次的影响（现有/拟变分钟数与差额），
再凭确认凭据原子提交（预演后任何相关状态已变即拒绝并要求重新预演）；现场网关断网恢复后可
**整批补传**同一设备的事件列表（全或无：逐项核对版本链，任一非法整批失败，成功时按最终状态
一次性重算受影响班次）；已结算班次还可登记**现场人工裁定**（唯一单号、整分钟区间、原因/说明，
可按单号撤销），裁定按生效顺序覆盖自动事件归因、撤销后回退；所有结果变化通过**全局递增游标**
的变更流（`GET /changes`）供下游报表持续同步每次结算、更正、裁定与撤销。下游报表**首次接入
或重建索引**时，可经 `GET /devices/{id}/shift-snapshots` 按**固定变更序号**取得班次归因快照
（只含与指定窗口相交、截至该序号已结算班次的最后修订），快照序号可直接作为变更流起始游标。
每个新生成的班次修订都会**同事务固化不可变的归因区间明细**（`GET /shifts/{id}/result/segments`）：
按起点排序、互不重叠的半开区间，逐段给出起止、原因及依据（生效裁定单号，或该区间内决定此原因的
事件来源与版本列表），使结算总分钟数的每一段都可解释；历史修订若无法可靠还原裁定状态，
明确返回明细不可用，不以当前状态推算。现场停机记录与已结算归因可经
`POST /devices/{id}/downtime-reconciliation` **按设备对账**：输入带时区整分钟的半开窗口、现场
停机区间列表与可选全局变更序号（缺省固定查询开始时的最新序号），系统侧按该序号取各相交班次
**最后修订的固化明细**（三类归因段并集）代表系统停机，仅在已结算班次覆盖的分钟内比较，按时间
顺序返回现场有/系统无、系统有/现场无的最大连续区间及分钟总数；覆盖外的现场分钟另计未对账、
不算差异，结果携带所用序号以便重放。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Docker Compose

## 快速启动

```bash
docker compose up --build
```

一条命令完成依赖安装、PostgreSQL 建库建表（含扩展与约束）与服务启动。

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | API 根地址 |
| `http://localhost:8000/docs` | Swagger 交互文档（可在线试调用） |
| `http://localhost:8000/health` | 健康检查 |
| `localhost:5432` | PostgreSQL（用户/密码 `postgres`，库 `downtime`） |

冒烟验证（覆盖下文全部流程）：

```bash
./scripts/smoke.sh                 # 或指定地址 ./scripts/smoke.sh http://localhost:8000
```

运行测试：

```bash
docker compose exec app pytest            # 算法单测 + API 端到端测试
```

## 配置

通过环境变量配置（`docker-compose.yml` 已为 `app` 服务注入）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/downtime` | SQLAlchemy 连接串 |
| `CURSOR_SECRET` | 内置开发默认值 | 快照续页游标的 HMAC 签名密钥；同一部署的多进程需一致，生产环境应显式注入随机值 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `downtime` | Compose 中数据库初始化参数 |

端口映射：`8000`（API）、`5432`（数据库），可在 `docker-compose.yml` 中调整。

## 业务规则

- **半开区间**：班次与事件均为 `[start, end)`；端点相接不算重叠、不漏分钟。
- **时间戳**：必须带时区且精确到整分钟（秒须为 `00`），否则 `422`。
- **班次**：同一设备的班次区间不可重叠（应用校验 + 数据库排他约束双保险），冲突 `409`。
- **事件版本链**：每个 `(设备, 来源)` 的版本从 1 开始连续递增。
  - 同来源 + 同版本 + 同内容 → 幂等返回（`200`，`idempotent=true`），不产生新数据；
  - 同版本异内容 → `409 VERSION_CONFLICT`；跳号 → `409 VERSION_GAP`；
  - 新版本即对旧版本的更正，当前生效事件 = 每个来源的最新版本。
- **预演-确认（上报前影响预览）**：`POST /events/preview` 输入与单条上报相同的
  候选事件（设备、来源、版本、整分钟起止、原因），按现有校验与版本链规则**只读预演**
  （不写入事件、修订或变更）：以旧版与候选版区间并集找出相关已结算班次，按当前全部
  来源最新版本及生效裁定计算各班次的**现有分钟数、拟变分钟数与差额**，结果不变的班次
  也列出（`changed=false`）；回执含不透明确认凭据 `confirm_token`。
  `POST /events/confirm` 凭该凭据提交：重新核对候选内容、来源版本链及影响结果的
  班次/事件/裁定状态，**任一已变 → `409 PREVIEW_STALE`，不写入任何数据**，需重新预演；
  状态未变才原子提交事件，并沿用现有更正规则仅为分钟数变化的班次生成修订与变更；
  相同确认重试遵循现有事件幂等语义（`200`，`idempotent=true`）。
- **断网补传（批量上报）**：`POST /events/batch` 一次提交同一设备的**非空**事件列表，
  各项字段与校验同单条上报（来源、版本、起止、原因）。
  - 按列表顺序核对每个来源的版本链：已存在的同版本同内容项为幂等重试；
    同版本异内容 / 跳号 / 未知设备 / 任一项非法 → **整批失败**（同事务回滚，
    不留下事件、修订或变更）；
  - 成功时先应用全部新版本，**仅以批前和批后各来源最新版本的区间并集**确定受影响
    的已结算班次，再按最终状态（全部来源最新版本 + 当前生效裁定）重算：
    每班次最多产生一次修订和一条变更（`trigger=correction`），分钟数未变则都不产生；
  - 回执逐项标明新建（`idempotent=false`）或幂等（`true`），并列出实际修订的班次
    及差额；全部项幂等时整体返回 `200`，否则 `201`；
  - 与单条上报及并发批次共用同一版本链行锁，并发下版本链保持连续无缺口。
- **归因**：窗口内每分钟只归因一次，重叠时 **保养 > 故障 > 待判**；结果含各原因分钟数
  与实际贡献分钟的事件版本列表。
- **现场人工裁定**（仅对已结算班次）：
  - 提交内容为 `单号 + 班次内整分钟半开区间 + 原因（保养/故障/待判）+ 说明`；
    区间必须落在班次 `[start, end)` 内（可与班次端点对齐），否则 `422 RULING_OUT_OF_RANGE`；
  - 裁定分钟**绝对覆盖**自动事件归因（含保养事件）；重叠裁定之间不比原因优先级，
    每分钟以**生效顺序最新的未撤销裁定**为准（生效顺序即落库先后），撤销后自然
    回退到此前仍有效的裁定，全部撤销后回到事件归因（按当前最新事件版本计算）；
  - 同单号同内容重试幂等（`200`），同单号异内容 `409 RULING_CONFLICT`；
    重复撤销幂等，未知班次 `404 SHIFT_NOT_FOUND`、未知单号 `404 RULING_NOT_FOUND`、
    班次未结算 `409 SHIFT_NOT_SETTLED`；
  - 裁定提交、撤销及裁定生效期间的事件更正，**仅在各原因分钟数变化时**产生班次修订
    （无修订时回执中 `revision=null` 明确标示）与 /changes 变更，历史修订永不重写。
- **结算与修订**：`POST /shifts/{id}/settle` 结算生成修订 1 并永久保留；迟到更正按事件时间
  （新旧版本区间的并集）重算受影响的已结算班次，现场裁定按上述规则一并参与重算；结果变化时
  追加新修订并记录与上一修订的差额（`delta`）；查询可用 `?revision=N` 指定历史修订。
- **归因区间明细**：每个新生成的修订都与修订**同事务**固化一份不可变的区间明细
  （`GET /shifts/{id}/result/segments?revision=N`，缺省为最新修订）：
  - 返回按起点排序、互不重叠的半开区间段，每段含起止、分钟数、原因及**依据**——
    生效裁定单号（`{"type":"ruling","ruling_no":...}`），或该区间内决定此原因的
    事件来源与版本列表（`{"type":"events","event_versions":[...]}`）；
  - 归因规则与结算一致：裁定按生效顺序覆盖事件，未裁定部分保养＞故障＞待判；
    在依据变化处拆段，相邻且原因、依据相同的段合并；未停机分钟不返回；
    各原因段分钟之和恒等于该修订的对应总数；
  - 明细只插入不修改：事件更正或裁定撤销只影响之后的新修订，旧修订明细保持原样；
  - 本结构引入前生成的历史修订未固化明细（当时的裁定状态无法可靠还原）：
    返回 `available=false`、`segments=null`，**不以当前状态推算**。
- **变更同步**：每次结算以及每次导致结果变化的更正 / 裁定 / 撤销产生一条变更记录
 （`seq` 全局递增，`trigger` 为 `settle` / `correction` / `adjudication` /
  `adjudication_revoke`），`GET /changes?cursor=&limit=` 按游标分页读取；修订与变更记录
  同一行存储、同事务提交/回滚，序号顺序即提交顺序，已返回条目不可变；空页保持原游标，
  非法游标/超限页大小返回 `422`。
- **错误**：未知设备 `404 DEVICE_NOT_FOUND`，非法区间 `422`，未结算 `404 RESULT_NOT_FOUND`，
  未知班次/修订 `404`。错误体统一为 `{"detail": {"code": "...", "message": "..."}}`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/devices` | 注册设备（同 id 重复提交幂等返回 200） |
| `GET` | `/devices` | 设备列表 |
| `POST` | `/shifts` | 创建班次（重叠 409 / 未知设备 404 / 非法区间 422） |
| `GET` | `/shifts/{id}` | 班次详情 |
| `POST` | `/events` | 上报事件版本（新建 201 / 幂等 200 / 冲突·跳号 409） |
| `POST` | `/events/preview` | 上报前只读预演：候选版本对已结算班次的影响（现有/拟变分钟数与差额），返回确认凭据 |
| `POST` | `/events/confirm` | 凭确认凭据原子提交候选版本（201 / 幂等 200 / 状态已变 409 需重新预演） |
| `POST` | `/events/batch` | 断网补传：同一设备事件列表整批上报（全或无；逐项回执新建/幂等；全部幂等 200，否则 201） |
| `POST` | `/shifts/{id}/settle` | 结算班次（重复调用幂等返回最新修订） |
| `GET` | `/shifts/{id}/result?revision=N` | 查询归因结果，缺省最新修订 |
| `GET` | `/shifts/{id}/result/segments?revision=N` | 该修订的归因区间明细（按起点排序的半开区间，含起止、原因及依据；历史修订未固化时返回不可用） |
| `GET` | `/shifts/{id}/revisions` | 全部修订及差额 |
| `POST` | `/shifts/{id}/adjudications` | 提交现场人工裁定（新建 201 / 幂等 200 / 单号冲突 409 / 未结算 409 / 越界 422） |
| `POST` | `/shifts/{id}/adjudications/{rulingNo}/revoke` | 按单号撤销裁定（首次 201 / 重复撤销 200 / 未知单号 404） |
| `GET` | `/shifts/{id}/adjudications` | 班次的全部裁定（含已撤销，按生效顺序） |
| `GET` | `/changes?cursor=&limit=` | 变更同步：按全局递增游标分页读取结算/更正/裁定/撤销变更 |
| `GET` | `/devices/{device_id}/shift-snapshots?start=&end=&seq=&limit=&cursor=` | 班次归因快照：按固定变更序号取与窗口相交、已结算班次的最后修订（首次接入/重建索引） |
| `POST` | `/devices/{device_id}/downtime-reconciliation` | 按设备对账现场停机与系统已结算归因（固定序号、差异与未对账分钟分别给最大连续区间） |
| `GET` | `/health` | 健康检查 |

原因枚举：`maintenance`（保养）、`failure`（故障）、`pending`（待判）。

错误体统一为 `{"detail": {"code": "...", "message": "..."}}`：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `DEVICE_NOT_FOUND` | 未知设备 |
| 404 | `SHIFT_NOT_FOUND` | 未知班次 |
| 404 | `RESULT_NOT_FOUND` | 班次尚未结算 |
| 404 | `REVISION_NOT_FOUND` | 指定修订不存在 |
| 409 | `SHIFT_OVERLAP` | 同设备班次区间重叠 |
| 409 | `VERSION_CONFLICT` | 同来源同版本但内容不同 |
| 409 | `VERSION_GAP` | 版本跳号（含首版本不为 1） |
| 409 | `PREVIEW_STALE` | 预演后相关状态（候选内容、来源版本链、班次/事件/裁定）已变，确认被拒绝且未写入任何数据，需重新预演 |
| 409 | `SHIFT_NOT_SETTLED` | 班次尚未结算，不能登记现场裁定 |
| 409 | `RULING_CONFLICT` | 裁定单号已存在但内容不同（含复用已撤销单号） |
| 404 | `RULING_NOT_FOUND` | 班次下未知裁定单号 |
| 422 | `RULING_OUT_OF_RANGE` | 裁定区间越出班次窗口 |
| 422 | `VALIDATION_ERROR` | 非法参数：区间无效、缺时区、未对齐整分钟、原因非法、游标/页大小越界等 |
| 422 | `INVALID_CONFIRM_TOKEN` | 确认凭据损坏、伪造或类型不符（如误用快照续页游标）；需重新预演获取凭据 |
| 422 | `INVALID_CURSOR` | 同步游标超过当前最大变更序号（客户端进度异常，需从较小游标重新同步）；快照续页游标损坏、伪造或与设备/窗口/快照序号不匹配 |
| 422 | `INVALID_SNAPSHOT_SEQ` | 快照序号超过当前已提交的最大变更序号（可取 0 或当前最大序号） |
| 422 | `INVALID_SEQ` | 对账序号超过当前已提交的最大变更序号（可取 0；省略则固定为当前最大序号） |
| 422 | `SEGMENTS_UNAVAILABLE` | 对账所选班次在该序号下的最后修订未固化归因明细（结构引入前的历史修订，当时裁定状态无法可靠还原），拒绝以当前状态推算 |

## 调用示例

以下完整走一遍主流程（假设服务运行于 `http://localhost:8000`）。

**1. 注册设备**

```bash
curl -X POST http://localhost:8000/devices \
  -H 'Content-Type: application/json' \
  -d '{"id": "CNC-01", "name": "加工中心1号"}'
# 201 {"id":"CNC-01","name":"加工中心1号"}
```

**2. 创建班次**（早班 08:00–16:00，+08:00）

```bash
curl -X POST http://localhost:8000/shifts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T16:00:00+08:00"}'
# 201 {"id":1,"device_id":"CNC-01","start":"2026-10-01T00:00:00Z","end":"2026-10-01T08:00:00Z"}
```

重叠班次返回 `409 {"detail":{"code":"SHIFT_OVERLAP", ...}}`；端点相接（如 16:00 起）允许。

**3. 上报停机事件**：保养 10:00–11:00

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"maintenance"}'
# 201 {"device_id":"CNC-01","source_id":"MES-1","version":1,"idempotent":false,"recomputed":[]}
```

**4. 幂等与版本校验**

```bash
# 原样重发 → 200，idempotent=true
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

# 同版本异内容 → 409 VERSION_CONFLICT
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}'

# 跳号（应为 2 却发 3）→ 409 VERSION_GAP
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":3,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'
```

**5. 再上报一条故障** 10:30–12:00（另一来源）

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":1,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'
```

**6. 结算班次**

```bash
curl -X POST http://localhost:8000/shifts/1/settle
```

```json
{
  "settled": true,
  "result": {
    "shift_id": 1, "revision": 1, "latest_revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "trigger": "settle", "..."
  }
}
```

> 10:30–11:00 与保养重叠，按优先级归保养；故障只计 11:00–12:00 的 60 分钟。

**7. 迟到更正**：故障实际只到 11:30，上报 `PLC-9` 的版本 2

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":2,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}'
```

```json
{
  "version": 2, "idempotent": false,
  "recomputed": [{
    "shift_id": 1, "revision": 2,
    "totals": {"maintenance": 60, "failure": 30, "pending": 0},
    "delta":  {"maintenance": 0,  "failure": -30, "pending": 0}
  }]
}
```

**8. 查询结果与修订**

```bash
curl http://localhost:8000/shifts/1/result                # 最新修订（revision=2）
curl 'http://localhost:8000/shifts/1/result?revision=1'   # 指定修订：结算时的原结果（failure=60）
curl http://localhost:8000/shifts/1/revisions             # 全部修订及每次差额
```

**9. 现场人工裁定**（仅对已结算班次；接续第 8 步，当前最新为修订 2）

```bash
# 9.1 提交裁定：10:30–11:00 现场改判为"待判"（原被保养覆盖）
curl -X POST http://localhost:8000/shifts/1/adjudications \
  -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-001",
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"pending","note":"传感器无记录，待质检复核"}'
```

```json
{
  "ruling_no": "R-20261001-001",
  "status": "active",
  "idempotent": false,
  "revision": 3,
  "latest_revision": 3,
  "revision_detail": {
    "revision": 3, "seq": 3,
    "totals": {"maintenance": 30, "failure": 30, "pending": 30},
    "delta":  {"maintenance": -30, "failure": 0,  "pending": 30},
    "trigger": "adjudication"
  }
}
```

> 裁定分钟绝对覆盖事件归因：10:30–11:00 原本归保养，现改判待判。
> 分钟数无变化时（如把本就是故障的分钟再裁定为故障），仍落库但 **不产生修订**，
> 回执为 `"idempotent": false, "revision": null, "revision_detail": null, "latest_revision": N`。

```bash
# 9.2 同单号同内容重试 → 幂等 200，无修订明确标示
curl -X POST http://localhost:8000/shifts/1/adjudications -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-001",
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"pending","note":"传感器无记录，待质检复核"}'
# 200 {"ruling_no":"R-20261001-001","status":"active","idempotent":true,
#      "revision":null,"latest_revision":3,"revision_detail":null}

# 9.3 同单号异内容 → 409 RULING_CONFLICT
curl -X POST http://localhost:8000/shifts/1/adjudications -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-001",
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"failure","note":"改判故障"}'
```

```bash
# 9.4 更晚生效的重叠裁定：10:45–11:15 改判故障，与上一条重叠的 10:45–11:00 以本条为准
curl -X POST http://localhost:8000/shifts/1/adjudications -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-002",
       "start":"2026-10-01T10:45:00+08:00","end":"2026-10-01T11:15:00+08:00",
       "reason":"failure","note":"现场确认为传动故障"}'
# → revision=4；重叠 15 分钟从 pending 变为 failure，11:00–11:15 的事件故障本就计 failure，不变

# 9.5 按单号撤销：重叠分钟回退至此前仍有效的 R-...001（pending），产生 adjudication_revoke 修订
curl -X POST http://localhost:8000/shifts/1/adjudications/R-20261001-002/revoke
# → {"status":"revoked","idempotent":false,"revision":5,
#    "revision_detail":{"trigger":"adjudication_revoke", ...}}

# 重复撤销 → 200 幂等，revision=null；未知单号 → 404 RULING_NOT_FOUND
curl -X POST http://localhost:8000/shifts/1/adjudications/R-20261001-002/revoke

# 9.6 查看班次全部裁定（含已撤销，按生效顺序）
curl http://localhost:8000/shifts/1/adjudications
```

裁定生效期间若有事件迟到更正，重算同样遵守裁定：裁定分钟不被事件改动，
事件只影响未被裁定覆盖的分钟，差额照常进入修订与 `/changes`。

**10. 错误示例**

```bash
# 非法区间（start ≥ end、秒非 00、缺时区）→ 422
curl -X POST http://localhost:8000/shifts -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'

# 未知设备 → 404 DEVICE_NOT_FOUND
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"NOPE","source_id":"X","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'
```

## 断网补传（批量上报）

现场网关断网期间缓存的事件，恢复后通过 `POST /events/batch` 一次补传：同一设备的非空
事件列表，各项字段与校验同单条上报（`source_id` / `version` / `start` / `end` / `reason`），
设备在批次级给出。整批核对、整批应用（全或无）。

**1. 补传批次**：新建与幂等混合（假设 `MES-1` 已报到版本 1，班次 1 已结算）

```bash
curl -X POST http://localhost:8000/events/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "device_id": "CNC-01",
    "events": [
      {"source_id":"MES-1","version":2,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"},
      {"source_id":"PLC-9","version":1,"start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"},
      {"source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}
    ]}'
# 201
```

```json
{
  "device_id": "CNC-01",
  "items": [
    {"source_id": "MES-1", "version": 2, "idempotent": false},
    {"source_id": "PLC-9", "version": 1, "idempotent": false},
    {"source_id": "MES-1", "version": 1, "idempotent": true}
  ],
  "recomputed": [{
    "shift_id": 1, "revision": 2,
    "totals": {"maintenance": 90, "failure": 30, "pending": 0},
    "delta":  {"maintenance": 30, "failure": 30, "pending": 0}
  }]
}
```

> 按列表顺序核对每个来源的版本链：`MES-1 v2`、`PLC-9 v1` 为连续新版本（新建），
> 断网期间重复缓存的 `MES-1 v1` 同版本同内容（幂等重试）。全部核对通过后一次性落库，
> 按最终状态重算受影响班次 —— 上例中保养延长到 11:30（+30），故障 11:30–12:00 计 30 分钟，
> 班次只产生**一次**修订（revision 2）和一条 `correction` 变更。

**2. 整批原样重发** → 全部项幂等，整体 `200`，`recomputed` 为空，不产生新数据：

```bash
curl -X POST http://localhost:8000/events/batch -H 'Content-Type: application/json' \
  -d '<同上请求体>'
# 200 {"items":[{"idempotent":true,...},...], "recomputed":[]}
```

**3. 整批失败（全或无）**：任一项非法，整批拒绝，不留下事件、修订或变更

```bash
# MES-1 v2 已存在但内容不同 → 409 VERSION_CONFLICT，同行的 PLC-9 v2 也不会落库
curl -X POST http://localhost:8000/events/batch -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","events":[
        {"source_id":"PLC-9","version":2,"start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T12:30:00+08:00","reason":"failure"},
        {"source_id":"MES-1","version":2,"start":"2026-10-01T09:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}]}'
# 409 {"detail":{"code":"VERSION_CONFLICT","message":"version 2 of source 'MES-1' already exists ..."}}

# 失败后版本链未推进：PLC-9 v2 仍可经单条或后续批次正常上报
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":2,
       "start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T12:30:00+08:00","reason":"failure"}'
# 201

# 跳号（应为 v3 却发 v4）→ 409 VERSION_GAP；未知设备 → 404 DEVICE_NOT_FOUND；
# 空列表或任一项缺时区/未对齐整分钟/原因非法 → 422 VALIDATION_ERROR
```

## 事件预演与确认（上报前影响预览）

运维人员上报某 `(设备, 来源)` 的下一版本前，可先用 `POST /events/preview` **只读预演**
候选版本对已结算班次的影响，确认无误后再用 `POST /events/confirm` **原子提交**。
预演请求体与单条上报完全相同（`device_id` / `source_id` / `version` / `start` / `end` /
`reason`），校验与版本链规则也相同（幂等 / 同版本异内容 409 / 跳号 409 / 未知设备 404 /
非法输入 422）；预演本身不写入事件、修订或变更，可反复调用。

**1. 预演**（接主流程第 7 步：班次 1 已结算，当前修订 2 为 保养 60 / 故障 30；
拟上报 `PLC-9` 的版本 3：故障延长到 12:30）

```bash
curl -X POST http://localhost:8000/events/preview \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":3,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:30:00+08:00",
       "reason":"failure"}'
# 200
```

```json
{
  "device_id": "CNC-01", "source_id": "PLC-9", "version": 3,
  "idempotent": false,
  "confirm_token": "eyJ0eXBlIjoiZXZlbnRfcHJldmlldyI...（不透明，原样回传）",
  "impacts": [{
    "shift_id": 1, "revision": 2,
    "current":  {"maintenance": 60, "failure": 30, "pending": 0},
    "proposed": {"maintenance": 60, "failure": 90, "pending": 0},
    "delta":    {"maintenance": 0,  "failure": 60, "pending": 0},
    "changed": true
  }]
}
```

> 相关班次按**旧版 ∪ 候选版区间并集**确定；`current` 为当前最新修订的分钟数，
> `proposed` 为应用候选版本后按全部来源最新版本及生效裁定算得的分钟数，
> `delta = proposed − current`；分钟数不变的班次也会列出（`changed=false`，
> 确认后不会产生修订）。同版本同内容的重复预演返回 `idempotent=true` 且
> `impacts=[]`（确认将幂等返回）。预演是只读的：结果、修订与 `/changes` 均不受影响。

**2. 确认提交**：原样回传 `confirm_token`

```bash
curl -X POST http://localhost:8000/events/confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirm_token":"eyJ0eXBlIjoiZXZlbnRfcHJldmlldyI..."}'
# 201
```

```json
{
  "device_id": "CNC-01", "source_id": "PLC-9", "version": 3,
  "idempotent": false,
  "recomputed": [{
    "shift_id": 1, "revision": 3,
    "totals": {"maintenance": 60, "failure": 90, "pending": 0},
    "delta":  {"maintenance": 0,  "failure": 60, "pending": 0}
  }]
}
```

> 确认时重新核对候选内容、来源版本链及影响结果的班次/事件/裁定状态，全部未变才
> 原子提交（事件落库 + 仅为分钟数变化的班次生成修订与变更，与单条上报的更正规则一致）。

**3. 相同确认重试** → 遵循现有事件幂等语义：`200`，`idempotent=true`，不产生新数据

```bash
curl -X POST http://localhost:8000/events/confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirm_token":"<同一凭据>"}'
# 200 {"version":3,"idempotent":true,"recomputed":[]}
```

**4. 预演后状态已变** → `409 PREVIEW_STALE`，不写入任何数据，需重新预演

```bash
# 预演 v4 之后、确认之前，该班次新增了一条现场裁定（或任一相关事件/班次状态变化）
curl -X POST http://localhost:8000/events/confirm \
  -H 'Content-Type: application/json' \
  -d '{"confirm_token":"<变化前预演得到的凭据>"}'
# 409 {"detail":{"code":"PREVIEW_STALE","message":"state has changed since the preview; ..."}}
# 处理：重新 POST /events/preview 获取新凭据后再确认
```

**5. 凭据非法**（损坏/伪造/误用快照续页游标）→ `422 INVALID_CONFIRM_TOKEN`；
同版本异内容、跳号与非法输入沿用既有约定（`409 VERSION_CONFLICT` / `409 VERSION_GAP` /
`422 VALIDATION_ERROR` / `404 DEVICE_NOT_FOUND`）。

## 归因区间明细（解释每个修订的分钟数构成）

`GET /shifts/{id}/result/segments?revision=N` 返回某班次指定修订（缺省最新）的归因区间明细：
结算总分钟数由哪些时间段、因何原因、依据什么得出。明细在修订生成时**同事务固化**、之后不可变。

**1. 查询最新修订的明细**（接主流程第 9.4 步：班次 1 当前修订 4 为 保养 30 / 故障 45 / 待判 15）

```bash
curl 'http://localhost:8000/shifts/1/result/segments'
```

```json
{
  "shift_id": 1, "revision": 4, "latest_revision": 4, "available": true,
  "segments": [
    {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T02:30:00Z", "minutes": 30,
     "reason": "maintenance",
     "basis": {"type": "events",
               "event_versions": [{"source_id": "MES-1", "version": 1, "reason": "maintenance"}]}},
    {"start": "2026-10-01T02:30:00Z", "end": "2026-10-01T02:45:00Z", "minutes": 15,
     "reason": "pending",
     "basis": {"type": "ruling", "ruling_no": "R-20261001-001"}},
    {"start": "2026-10-01T02:45:00Z", "end": "2026-10-01T03:15:00Z", "minutes": 30,
     "reason": "failure",
     "basis": {"type": "ruling", "ruling_no": "R-20261001-002"}},
    {"start": "2026-10-01T03:15:00Z", "end": "2026-10-01T03:30:00Z", "minutes": 15,
     "reason": "failure",
     "basis": {"type": "events",
               "event_versions": [{"source_id": "PLC-9", "version": 2, "reason": "failure"}]}}
  ]
}
```

> 段按起点排序、互不重叠（半开区间，UTC 输出）；只含停机分钟。
> 逐段依据：10:00–10:30 由保养事件 MES-1 v1 决定；10:30–10:45 由生效裁定
> R-20261001-001（待判）决定；10:45–11:15 由更晚生效的 R-20261001-002（故障）覆盖；
> 11:15–11:30 回到事件归因（PLC-9 v2 故障）。各原因段分钟之和 = 该修订总数
> （保养 30 / 故障 45 / 待判 15）。

**2. 指定历史修订**：明细在固化后不可变 —— 撤销 R-20261001-002（产生修订 5）后，
修订 4 的明细仍原样返回

```bash
curl -X POST http://localhost:8000/shifts/1/adjudications/R-20261001-002/revoke
curl 'http://localhost:8000/shifts/1/result/segments?revision=4'   # 内容同上，不变
curl 'http://localhost:8000/shifts/1/result/segments?revision=5'   # 回退后的新明细
```

**3. 本结构引入前生成的历史修订**：从旧版本升级上来的部署中，升级前已存在的修订
未固化明细，且当时的裁定状态无法可靠还原 —— 明确返回不可用
（`available=false`、`segments=null`），不以当前状态推算；升级后新生成的修订
（含上述主流程中的全部修订）均正常返回明细

```bash
curl 'http://localhost:8000/shifts/1/result/segments?revision=1'   # 假设修订 1 为升级前生成
```

```json
{"shift_id": 1, "revision": 1, "latest_revision": 5, "available": false, "segments": null}
```

**4. 错误语义**沿用现有约定：未知班次 `404 SHIFT_NOT_FOUND`、未结算
`404 RESULT_NOT_FOUND`、未知修订 `404 REVISION_NOT_FOUND`、非法修订号 `422 VALIDATION_ERROR`。

## 变更同步（下游报表订阅）

`GET /changes` 把每次**结算**与每次**导致结果变化的更正 / 现场裁定 / 撤销**暴露为按全局
递增序号 `seq` 排序的变更流，供下游报表持续同步。每条变更包含：班次（id、设备、起止）、
修订号、各原因分钟数（`totals`）、与上一修订的差额（`delta`）、触发类型（`trigger`：
`settle` / `correction` / `adjudication` / `adjudication_revoke`）、所依据的事件版本与
产生时间。

**游标语义**

- 请求参数 `cursor` 表示"已消费的最大 `seq`"，返回 `seq > cursor` 的变更，按 `seq` 升序；
  `cursor=0`（或缺省）表示从头回放全部既有修订。
- 每页返回 `next_cursor`（本页最后一条的 `seq`），作为下一次请求的 `cursor` 即可翻页。
- **空页保持原游标**：已追平时返回 `{"changes": [], "next_cursor": <原 cursor>}`，
  客户端可用同一游标轮询等待新变更。
- 页大小 `limit` 缺省 100，上限 500。

**一致性保证**

- 变更记录与对应修订是同一行（`shift_results.seq`），同一事务提交或回滚，不会半截可见；
- 序号由单例计数器在写事务内加锁分配，**序号顺序 == 提交顺序**：并发结算/更正时，
  游标不会越过尚未可见的较早变更；
- 条目只追加不修改，重读已返回的内容不变；
- 幂等重复上报/结算、结果未变化的更正、分钟数无变化的裁定/撤销均**不产生**变更记录。

**参数约束**：`cursor ≥ 0` 且不得超过当前最大序号；`1 ≤ limit ≤ 500`。
非法游标或超限页大小返回 `422`（只读请求，不影响任何同步进度）。

**调用示例**（接上面主流程，此时已有结算 seq=1、更正 seq=2 两条变更）

```bash
# 1) 从头回放：第一页（每页 1 条）
curl 'http://localhost:8000/changes?cursor=0&limit=1'
```

```json
{
  "changes": [{
    "seq": 1, "shift_id": 1, "device_id": "CNC-01",
    "shift_start": "2026-10-01T00:00:00Z", "shift_end": "2026-10-01T08:00:00Z",
    "revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "trigger": "settle", "created_at": "..."
  }],
  "next_cursor": 1
}
```

```bash
# 2) 用上一页的 next_cursor 翻页
curl 'http://localhost:8000/changes?cursor=1&limit=100'
# → changes[0] 为 seq=2 的更正（trigger="correction"，delta.failure=-30），next_cursor=2

# 3) 已追平：空页保持原游标，可轮询等待
curl 'http://localhost:8000/changes?cursor=2'
# → {"changes": [], "next_cursor": 2}

# 4) 非法参数 → 422，同步进度不受影响
curl 'http://localhost:8000/changes?cursor=-1'     # VALIDATION_ERROR
curl 'http://localhost:8000/changes?limit=1000'    # VALIDATION_ERROR（上限 500）
curl 'http://localhost:8000/changes?cursor=999'    # INVALID_CURSOR（超过当前最大序号）
```

## 班次归因快照（首次接入 / 重建索引）

`GET /devices/{device_id}/shift-snapshots` 供下游报表**首次接入**或**重建索引**时，按一个
**固定变更序号**取得该设备在指定时间窗口内各班次的归因快照，随后再以该序号为起点衔接
`GET /changes` 做持续增量同步。

**请求参数**（均为 query 参数）

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `device_id` | 是（路径） | 设备标识；未知设备 `404 DEVICE_NOT_FOUND` |
| `start` / `end` | 是 | 半开时间窗口 `[start, end)`，带时区且精确到整分钟；与班次区间**相交**（端点相接不算）即纳入 |
| `limit` | 否 | 页大小，缺省 100，范围 `1–500` |
| `seq` | 否 | 快照变更序号；**缺省时以本次请求读取时已提交的最大序号为准**（整个快照固定在该序号） |
| `cursor` | 否 | 上一页返回的不透明续页游标，首页不传 |

**快照语义**

- 只列出与窗口相交、截至 `seq` **已结算**的班次；每班次取 **序号 ≤ `seq` 的最后修订**，
  返回班次标识（`shift_id`）、班次区间、修订号（`revision`）与三类原因分钟数（`totals`）；
- 每页回传 `snapshot_seq`；翻页按班次标识（id）稳定升序，`next_cursor` 为**不透明**续页游标
  （HMAC-SHA256 签名，内含设备、窗口、快照序号与本页最后班次 id），**末页为 `null`**；
  空快照（无匹配班次或 `seq=0` 时无任何结算）首页即空页，`next_cursor=null`；
- **快照不可变**：只读永久保留的历史修订，且按班次主键键集翻页 —— 后续更正、裁定、撤销或
  新结算只追加更大序号的修订，不会改变同一快照的任何后续页（即便续页请求不再显式带 `seq`，
  游标内的序号也保持视图固定）；想追最新数据请不带游标/序号重新发起首页请求；
- 返回的 `snapshot_seq` 可**直接作为 `GET /changes?cursor=` 的起始游标**：快照含 `seq ≤ 快照序号`
  的状态，变更流再返回 `seq > 快照序号` 的增量，两者无缝衔接、不重不漏。

**错误**

- `seq` 超过当前已提交的最大序号 → `422 INVALID_SNAPSHOT_SEQ`（`seq=0` 合法，表示尚无变更时的空快照）；
- 续页游标损坏/伪造，或与本次的设备、窗口、快照序号不一致 → `422 INVALID_CURSOR`
  （更换筛选条件须丢弃旧游标、从首页重新开始）；
- 窗口缺时区/未对齐整分钟/`start ≥ end`、页大小越界、`seq < 0` → `422 VALIDATION_ERROR`。

**调用示例**（接前面主流程：班次 1 结算 seq=1、班次 2 结算 seq=2、班次 1 更正 seq=3；
窗口取 10/1–10/4，每页 1 条）

```bash
# 1) 首页：不指定 seq → 快照固定为当前已提交最大序号 3
curl 'http://localhost:8000/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&limit=1'
```

```json
{
  "device_id": "CNC-01",
  "start": "2026-10-01T00:00:00+08:00",
  "end": "2026-10-04T00:00:00+08:00",
  "snapshot_seq": 3,
  "shifts": [{
    "shift_id": 1,
    "start": "2026-10-01T00:00:00Z", "end": "2026-10-01T08:00:00Z",
    "revision": 2,
    "totals": {"maintenance": 60, "failure": 30, "pending": 0}
  }],
  "next_cursor": "eyJhZnRlciI6MSwiZGV2aWNlX2lkIjoiQ05DLTAxIi...（不透明，原样回传）"
}
```

```bash
# 2) 续页：原样回传 next_cursor（可不再传 seq；窗口仍须一致），末页 next_cursor=null
curl -G 'http://localhost:8000/devices/CNC-01/shift-snapshots' \
  --data-urlencode 'start=2026-10-01T00:00:00+08:00' \
  --data-urlencode 'end=2026-10-04T00:00:00+08:00' \
  --data-urlencode 'limit=1' \
  --data-urlencode 'cursor=eyJhZnRlciI6MSwiZGV2aWNlX2lkIjoiQ05DLTAxIi...'
# → shifts=[{shift_id:2, revision:1, totals:{maintenance:60,...}}], next_cursor=null
#   未结算的班次 3 不出现；与窗口仅端点相接/不相交的班次也不出现

# 3) 重建索引：显式固定历史序号 seq=1 —— 班次 1 取修订 1（结算时状态），
#    班次 2 当时尚未结算故不列出；之后无论数据怎么变，带 seq=1 重读结果一致
curl 'http://localhost:8000/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&seq=1'

# 4) 翻完快照后，以 snapshot_seq 衔接变更流持续同步（只收 seq>3 的增量）
curl 'http://localhost:8000/changes?cursor=3'

# 5) 错误示例
# seq 超范围 → 422 INVALID_SNAPSHOT_SEQ
curl 'http://localhost:8000/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&seq=99'
# 游标换窗口（或损坏/伪造）→ 422 INVALID_CURSOR
curl -G 'http://localhost:8000/devices/CNC-01/shift-snapshots' \
  --data-urlencode 'start=2026-10-02T00:00:00+08:00' --data-urlencode 'end=2026-10-04T00:00:00+08:00' \
  --data-urlencode 'seq=3' --data-urlencode 'cursor=<上一页游标>'
# 缺时区（未对齐整分钟、start≥end、limit=0/501 同理）→ 422 VALIDATION_ERROR
curl 'http://localhost:8000/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00&end=2026-10-04T00:00:00%2B08:00'
# 未知设备 → 404 DEVICE_NOT_FOUND
curl 'http://localhost:8000/devices/NOPE/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00'
```

## 现场停机对账（按设备，按固定变更序号）

现场停机记录（如班组纸质/网关补录）与系统**已结算归因**可能不一致：
`POST /devices/{device_id}/downtime-reconciliation` 按设备、按一个**固定变更序号**对账，
只比较该序号下**已结算班次覆盖**的分钟，输出两侧差异的最大连续区间，序号随结果回传以便重放。

**请求体**（JSON）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `start` / `end` | 是 | 半开对账窗口 `[start, end)`，必须带时区且精确到整分钟，`start < end` |
| `field_intervals` | 否 | 现场停机区间列表（每项整分钟半开），无需落在窗口/班次内；服务端先**裁剪到窗口并合并重叠** |
| `seq` | 否 | 固定变更序号；**缺省时以请求开始时已提交的最大序号为准**（整个对账固定在该序号） |

**对账语义**

- 系统侧：取与窗口**相交**（半开，端点相接不算）的班次中、截至 `seq` **已结算**的班次，
  每班选 **序号 ≤ `seq` 的最后修订**（与班次快照同一选择规则，历史修订不可变）；
  **系统停机 = 这些修订固化归因明细（保养/故障/待判三类段）的并集** —— 三类原因一律视为停机，
  对账不再区分原因；
- **已结算覆盖** = 各相交班次区间与窗口交集的并集（班次内未停机分钟也在对账范围内）；
- 逐分钟比较只在已结算覆盖内进行，结果均为按时间顺序的**最大连续半开区间**：
  - `field_only`：现场有、系统无（差异），合计 `field_only_minutes`；
  - `system_only`：系统有、现场无（差异），合计 `system_only_minutes`；
  - `unreconciled`：现场记录落在**任何已结算班次之外**的分钟（班次间隙、未结算班次、窗口内
    无班次时段），**另计未对账、不算差异**，合计 `unreconciled_minutes`；
- `settled_minutes` 为窗口内已结算覆盖总分钟（对账范围）；`reconciled_shifts` 列出实际参与
  对账的班次及其所选修订/序号，便于核对；`seq` 为本次实际使用的序号 —— 之后数据再变化，
  携带同一 `seq` 重放结果不变。

**错误**

- `seq` 超过当前已提交最大序号 → `422 INVALID_SEQ`（`seq=0` 合法：无任何结算时空对账，
  现场分钟全部计未对账）；
- 所选修订为明细结构引入前的历史修订（`segments_available=FALSE`，当时裁定状态无法可靠
  还原）→ `422 SEGMENTS_UNAVAILABLE`，**不以当前状态推算替代**；
- 未知设备 → `404 DEVICE_NOT_FOUND`；窗口/现场区间缺时区、未对齐整分钟、`start ≥ end`、
  `seq < 0` → `422 VALIDATION_ERROR`。

**调用示例**（设备 `CNC-01`，10/1 早班 08:00–16:00 已结算；系统固化明细为故障 10:00–11:00）

```bash
# 现场记录：09:30–10:30 停机（与系统重叠 10:00–10:30）；不指定 seq → 固定当前最新序号
curl -X POST http://localhost:8000/devices/CNC-01/downtime-reconciliation \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "2026-10-01T00:00:00+08:00",
    "end":   "2026-10-02T00:00:00+08:00",
    "field_intervals": [
      {"start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
    ]
  }'
```

```json
{
  "device_id": "CNC-01",
  "start": "2026-10-01T00:00:00+08:00",
  "end": "2026-10-02T00:00:00+08:00",
  "seq": 1,
  "settled_minutes": 480,
  "field_only": [
    {"start": "2026-10-01T01:30:00Z", "end": "2026-10-01T02:00:00Z", "minutes": 30}
  ],
  "field_only_minutes": 30,
  "system_only": [
    {"start": "2026-10-01T02:30:00Z", "end": "2026-10-01T03:00:00Z", "minutes": 30}
  ],
  "system_only_minutes": 30,
  "unreconciled": [],
  "unreconciled_minutes": 0,
  "reconciled_shifts": [{"shift_id": 1, "revision": 1, "seq": 1}]
}
```

> 现场 09:30–10:00 在已结算班次内但系统无停机 → 现场差异；系统 10:30–11:00 现场无记录
> → 系统差异；两侧重叠的 10:00–10:30 一致，不出现。

```bash
# 覆盖外的现场分钟（班次外 / 未结算班次 / 班次间隙）→ unreconciled，不算差异
curl -X POST http://localhost:8000/devices/CNC-01/downtime-reconciliation \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00",
       "field_intervals":[
         {"start":"2026-10-01T12:00:00+08:00","end":"2026-10-01T13:00:00+08:00"},
         {"start":"2026-10-01T20:00:00+08:00","end":"2026-10-01T21:00:00+08:00"}]}'
# 12:00–13:00 在班次 08–16 内（系统无停机）→ field_only；
# 20:00–21:00 在所有已结算班次之外 → unreconciled

# 固定历史序号重放：即使之后又有更正/裁定，结果与当时一致
curl -X POST http://localhost:8000/devices/CNC-01/downtime-reconciliation \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00",
       "seq":1,"field_intervals":[]}'

# 错误示例
# seq 越界 → 422 INVALID_SEQ；历史修订无明细 → 422 SEGMENTS_UNAVAILABLE
curl -X POST http://localhost:8000/devices/CNC-01/downtime-reconciliation -H 'Content-Type: application/json' \
  -d '{"start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","seq":999,"field_intervals":[]}'
# 窗口缺时区 / 未对齐整分钟 / start≥end、现场区间非法 → 422 VALIDATION_ERROR；未知设备 → 404
```

## 设计要点

- **归因算法**（`app/attribution.py`，纯标准库可独立测试）：按原因收集区间并裁剪到班次窗口；
  保养区间合并后全量生效，故障区间扣除保养覆盖，待判区间再扣除保养与故障覆盖 —— 区间运算
  保证每分钟只归因一次；`used_versions` 只保留裁剪后仍对有效区间有贡献的事件版本。
  现场裁定（`attribute_with_rulings`）逐分钟取生效顺序（seq，即落库 id）最新的未撤销裁定，
  裁定分钟绝对覆盖事件；事件只在扣除全部裁定区间后的剩余窗口内做上述优先级归因，
  因此撤销最新裁定后重叠分钟自然回退到此前裁定或事件。
- **版本链与幂等**：`event_sources` 表保存每个 `(设备, 来源)` 的 `last_version` 与内容哈希，
  上报时 `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE` 串行化校验；
  内容哈希 = `sha256(start_utc|end_utc|reason)`。
- **裁定与撤销**：`manual_adjudications` 保存裁定原文与 `active/revoked` 状态（撤销留痕、
  行不删除），`(shift_id, ruling_no)` 唯一；内容哈希 = `sha256(start_utc|end_utc|reason|note)`，
  同单号同内容幂等、异内容冲突（含复用已撤销单号）。提交/撤销在**班次行 `FOR UPDATE` 锁内**
  完成，故同班次的结算、事件更正重算与所有裁定写入相互串行，裁定 id 顺序即对其他事务可见的
  生效顺序；并发不丢失更新。
- **迟到更正**：新版本落库后，按"旧版本区间 ∪ 新版本区间"找出受影响的已结算班次，用全部
  来源的最新版本与当前生效裁定重算；仅当分钟数变化时追加新修订（避免空修订），差额 = 新 − 旧。
- **预演-确认**：预演只读 —— 不补建来源行、不加锁、不写任何数据，按"旧版 ∪ 候选版"区间
  并集找出相关已结算班次，以候选版本替换其来源的当前最新版本后按全部来源最新版本与生效
  裁定计算拟变分钟数。确认凭据为 `base64url(JSON).HMAC-SHA256`（与快照游标同一密钥，载荷
  含类型标识、候选内容、来源版本链位置及各相关班次的"最新修订号 + 相交最新事件版本 +
  裁定状态"指纹）。确认在与单条上报相同的来源行锁与班次行锁内逐项复核：任一状态已变即
  `409 PREVIEW_STALE` 整体拒绝（未写入任何数据）；全部一致才同事务落库事件并复用
  `_recompute_affected_shifts` 重算，相同凭据重试走版本链幂等分支（`200`），并发确认
  同一凭据天然串行化为一次新建 + 其余幂等。
- **断网补传**：批量接口与单条共用同一套版本链校验与内容哈希；批次内涉及的来源行按
  `source_id` 固定顺序 `FOR UPDATE` 加锁（与单条上报、其他批次相互串行，版本链连续无缺口），
  全部项核对通过前不写入任何数据，随后同事务应用全部新版本并一次性重算 —— 受影响班次按
  "各来源批前最新 ∪ 批后最新"区间并集确定（中间版本区间不参与），每班次至多重算一次；
  任一项失败即整体回滚，事件、修订、变更都不留痕。
- **并发与一致性**：班次不重叠由 PostgreSQL 排他约束（`btree_gist` + `tstzrange '[)'`）兜底；
  结算、更正重算与裁定写入对班次行加锁；事件版本链对来源行加锁，全流程单事务提交。
- **不可变历史**：`shift_results` 只追加不修改，`(shift_id, revision)` 唯一；裁定撤销只改
  自身状态并追加修订，从不改写历史修订与已返回的变更条目。
- **归因区间明细**：`shift_result_segments` 与修订同事务写入、之后只读不改
  （`(shift_id, revision, seg_no)` 唯一，复合外键指向 `shift_results`）。
  计算（`attribution.attribute_segments`）以全部事件/裁定端点把班次窗口切成
  覆盖集合恒定的基本区间：逐区间取生效顺序最新的裁定（依据 = 裁定单号），
  无裁定区间按保养＞故障＞待判归因（依据 = 覆盖该区间且原因相符的事件版本列表）；
  相邻且原因、依据相同的区间合并，未覆盖分钟不产生段 —— 与
  `attribute_with_rulings` 同一套规则，各原因段分钟之和恒等于修订总数。
  `shift_results.segments_available` 标记是否固化了明细：既有库启动迁移时
  历史修订一律回填 `FALSE`（当时的裁定状态无法可靠还原），查询明确返回
  明细不可用；新修订恒为 `TRUE`。
- **变更订阅**：变更流直接读取修订表 —— `shift_results.seq` 即全局变更序号，修订与变更
  天然同事务提交/回滚；序号由单例计数器行（`change_counter`）在写事务内
  `UPDATE ... RETURNING` 递增分配，行锁持有至提交，故序号顺序 == 提交顺序，同步游标不会越过
  尚未可见的较早变更；条目不可变，重读一致。既有库启动时自动迁移：为 `shift_results`
  回填 `seq`（按原自增 id 顺序）、加宽 `trigger` 列以容纳裁定触发器，并以最大序号初始化计数器。
- **班次快照**：首次接入/重建索引按固定序号读不可变历史 —— 每班次用
  `seq ≤ snapshot_seq` 中 `revision` 最大的修订行（相关子查询每组取最大修订号再回连），
  窗口按半开区间相交过滤，按 `shifts.id` 键集（keyset）翻页；因为修订只追加、历史行不重写、
  班次 id 单调，快照页天然对后续写入免疫。续页游标为 `base64url(JSON).HMAC-SHA256`
  （载荷含设备、UTC 归一化窗口、快照序号与本页末班次 id，密钥取 `CURSOR_SECRET`），
  验签并逐项比对本次筛选条件，损坏/伪造/不匹配一律 `422 INVALID_CURSOR`；
  快照序号与 `/changes` 游标同一序列，可直接衔接增量同步。
- **现场停机对账**（`app/reconciliation.py`，纯标准库可独立测试）：现场区间裁剪到窗口并
  合并后，与"系统停机并集"（各相交班次按固定序号所选最后修订的固化明细三类段合并，
  只读不可变历史）做半开区间差集；已结算覆盖为各班次窗口交集的并集 ——
  现场差异 = 现场 ∩ 覆盖 − 系统，系统差异 = 系统 − 现场（系统段恒在覆盖内），
  未对账 = 现场 − 覆盖；差集结果天然按时间排序且为最大连续区间，端点相接不漏分钟。
  序号选取复用班次快照的"每组最大修订号"子查询；所选修订 `segments_available=FALSE`
  （明细结构引入前的历史修订，裁定状态无法可靠还原）或序号越界时整体拒绝，
  绝不用当前状态替代；响应回传固定序号与各班次所选修订，可原样重放。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  services.py     # 业务逻辑：版本链、幂等、结算、更正重算
  attribution.py  # 归因算法（半开区间运算，纯标准库）
  reconciliation.py # 现场-系统对账算法（裁剪/覆盖/差集，纯标准库）
  models.py       # SQLAlchemy 模型
  schemas.py      # Pydantic 请求/响应与校验
  db.py           # 引擎、会话、建库建表（含排他约束）
  config.py       # 环境变量配置
tests/
  test_attribution.py    # 算法单测（含裁定覆盖/重叠/回退与区间明细拆段合并，无外部依赖，可 python3 直接跑）
  test_reconciliation.py # 对账算法单测（裁剪合并/覆盖差集/未对账/最大连续区间，无外部依赖，可 python3 直接跑）
  test_api.py            # API 端到端测试（需 PostgreSQL）
  test_changes.py        # 变更同步（游标分页/回放/并发）端到端测试（需 PostgreSQL）
  test_adjudications.py  # 现场裁定（提交/幂等/冲突/撤销/联动/并发）端到端测试（需 PostgreSQL）
  test_event_batch.py    # 断网补传（整批核对/全或无/重算/并发）端到端测试（需 PostgreSQL）
  test_event_preview.py  # 事件预演与确认（只读预演/凭据/状态复核/幂等重试/并发）端到端测试（需 PostgreSQL）
  test_shift_snapshots.py  # 班次快照（固定序号/相交过滤/稳定翻页/不可变/游标校验）端到端测试（需 PostgreSQL）
  test_result_segments.py  # 归因区间明细（固化/不可变/依据形态/历史不可用/错误语义）端到端测试（需 PostgreSQL）
  test_downtime_reconciliation.py  # 现场停机对账（固定序号/最后修订明细并集/覆盖内外/未对账/序号越界与明细不可用）端到端测试（需 PostgreSQL）
scripts/smoke.sh         # 冒烟脚本
Dockerfile / docker-compose.yml / requirements.txt
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/downtime'
uvicorn app.main:app --reload --port 8000
pytest
```
