# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计、通知和监管上报版本，以及召回成效复盘（应修/已修/待通知/失联/超期归集、版本留档、失联车主跟进）。

代码按职责拆分：`common.py` 为共享基础，`effectiveness.py` 负责成效统计、版本档案与失联名单，`app.py` 负责主业务流程与 HTTP 接口——统计、档案和接口分开处理。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/scope`：调整召回范围并生成新版本通知/上报。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修；对已确认记录执行 `flag` 即复核退回，已修数随之减少、零件回补。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/recalls/{id}/effectiveness`：成效复盘。按召回和范围版本归集应修、已修、待通知、失联、超期车辆，并按国家、车型拆分；车辆多次转移只按当前归属计一次。`current` 为当前版本实时账目，`archived` 为范围调整时冻结的旧版本账目（新旧版本各自留档）；`?overdue_days=` 可调整超期口径（默认 90 天）。
- `POST /api/recalls/{id}/notify`：标记当前范围版本通知已发出（可按 `vin` 指定单车）。
- `POST /api/recalls/{id}/contacts`、`GET /api/recalls/{id}/unreachable`：登记车主联系结果、查看失联名单。名单记录每次联系，连续三次未果转监管跟进（`escalated`），联系成功或完成修复即退出名单。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可字符串模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
