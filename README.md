# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计，以及通知和监管上报版本。

## 分层

- `app.py`：HTTP 接口层（鉴权头、路由、请求/响应）。
- `service.py`：领域服务（召回流转、维修复核、失联跟进、成效入口）。
- `archives.py`：档案层（范围版本留档与车辆名册、失联名单与每次联系记录）。
- `stats.py`：统计层（只读，按召回/范围版本实时归集散桶与国家、车型分组）。
- `store.py`：持久化层（建表、旧库档案回填、审计）。
- `common.py`：时间、JSON、错误等公共工具。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。修复方案可带 `deadline`（YYYY-MM-DD）用于超期统计。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/scope`：调整召回范围并生成新版本通知/上报，新旧版本各自留档。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修（`confirm`/`flag`/`reject`）。
- `POST /api/repairs/{id}/reject`：复核退回（含已确认记录）：已修数随即减少、零件退库，因此退出的失联档案恢复跟进。
- `POST /api/recalls/{id}/notify`：通知实际送达后出队，待通知数减少。
- `POST /api/recalls/{id}/unreachable`：把长期联系不上的车主加入失联名单。
- `POST /api/unreachable/{case_id}/contact`：记录每次联系（`failed`/`reached`）；连续三次未果自动转监管跟进，联系成功或完成修复即退出名单。
- `GET /api/recalls/{id}/effectiveness`：成效复盘，按召回跨版本去重归集；加 `?scope_version=N` 查看单个版本账本。
- `GET /api/followups`、`GET /api/recalls/{id}/followups`：监管跟进名单。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 成效复盘口径

- 按“召回 × 范围版本”归集应修、已修、待通知、失联（跟进中/转监管）和超期车辆，并按国家、车型分组；召回层级跨版本并集，同一辆车转移或重复入册只算一次。
- 已修只统计 `confirmed` 维修；复核退回（`reject`）后已修数立即下降，车辆回到待修/超期口径。
- 范围调整不改旧账：每个版本保留独立的范围快照与车辆名册；发布后登记且符合历史版本范围的车辆幂等补入名册。
- 失联名单保留全部联系记录；三次连续未果转监管，联系成功或修复完成关闭，复核退回则恢复到退出前状态。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可字符串模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
