# 设备外协交还与排产权

本仓库包含两部分：

1. `data/equipment_20261205.json`：各参与方的合成交换报文快照，含未核认、冲突和迟到事实；
2. 一个仅依赖 Python 3.11 标准库的后端：按设备协调维护委托、外协接手、分区记录、厂区签收、控制台回执、重新对账和排产开批核验。

## 运行与测试

```sh
python3 -m unittest discover -s tests -v
python3 app.py --port 8000
# 或 PORT=8000 python3 -m backend.server
```

启动后访问 <http://127.0.0.1:8000/> 查看接口索引。服务启动时会自动载入 `data/equipment_20261205.json` 作为联调样本；`POST /api/reset` 可恢复样本，请求体 `{"seed": false}` 可清空为空白内存状态。

原交换资料的只读静态服务仍可单独运行：

```sh
python3 -m http.server 8040 --bind 127.0.0.1 --directory data
```

## 核心状态边界

服务绝不把以下状态互相替代：

- **维修平台完工/外协申请交还**：只形成交还申请，不转移责任，也不是排产许可；
- **厂区签收**：表示维护责任已由厂区接回，按签收区段更新当前责任；
- **控制台回执**：独立报告 `owner`、`mode`、`revision`、`scopes` 和 `request_key`。只有 `owner=shop`、`mode=production` 且区段覆盖本批实际所需范围时才允许排产；
- **开批核验**：即使之前领取过放行，`POST /api/workshop/batches/{id}/start` 仍重新计算当前责任和最新回执。

`verify`、`maintain`、外协持有方、缺少区段、等待回执均为阻止条件。请求超时或 `unknown` 不是最终失败；只有控制台明确拒绝才是终态失败。迟到/乱序回执会原样保存为独立回执，并按设备内递增 `revision`（相同版本再参考 `applied_at`）确定各区段当前控制台状态。旧委托的低版本回执不能覆盖后续维护对同区段的更高版本控制状态；未交清的外协责任也会直接阻止排产。

## 主要接口

### 办公室：计划、委托和交接

- `POST /api/office/mandates`：业主一次提交设备、工艺区段并签发委托（也可用 `/api/orders`）；
- `GET /api/office/orders?device_id=D1`；
- `POST /api/office/orders/{id}/confirm`：厂区确认窗口和可进入范围；
- `POST /api/orders/{id}/takeover`：合格外协人员接手；
- `POST /api/orders/{id}/records`：多人提交检查/维修记录，分包人只能写被授予区段；
- `POST /api/orders/{id}/expand`：评估额外故障导致的扩大停用；
- `POST /api/office/handovers`：外协申请交还，或带 `sign_by` 时由厂区签收；
- `POST /api/returns/{id}/sign`；
- `GET/POST /api/office/batches`：排产计划；
- `POST /api/office/batches/{id}/alternatives`：确认替代设备计划；
- `POST /api/orders/{id}/impact/confirm`：确认被挤掉的未开批批次及替代安排。

常见资源别名也已挂载：`/jobs`、`/production`、`/plans`、`/handovers`、`/mandates`。

### 设备/控制台回传接口

- `POST /api/device/receipts`
- `POST /api/console/receipts`
- `POST /api/device/callbacks`
- `GET /api/device/receipts?device_id=D1&request_key=x1`
- `GET /api/dispatches?request_key=x1`

请求体遵循《设备交接回执》字段：`id/device/job/revision/owner/mode/scopes/applied_at/arrived_at/request_key`。也支持 `{"controls": [...]}` 或 JSON 数组批量回放。

### 车间查询与开批

- `GET /api/workshop/devices/D1/availability?scopes=feed,core&at=12:30`
- `GET /api/workshop/batches/P2/check?at=12:30`
- `POST /api/workshop/batches/P2/claim`：按该批 `required_area` 领取放行；
- `POST /api/workshop/batches/P2/start`：开批前重新核验；未确认时返回 `409`、具体 `conditions`、中文等待/改设备提示以及可行的 `suggested_device`。

### 值班员后台与对账

- `GET /api/duty`：设备当前责任、全部独立回执、排产等待条件；
- `GET /api/duty/devices/D1`；
- `GET /api/duty/responsibility?device_id=D1`；
- `GET /api/duty/receipts?device_id=D1`；
- `GET /api/duty/waits`；
- `POST /api/reconcile`，体例如 `{"device_id":"D1","request_key":"x2","recover":true}`。

## 联调样本中的预期结论

- `O2` 可接手 `J1`（`feed`）和扩展委托 `J3`（`core`），但不能合法接手含 `out` 且到 `16:00` 的 `J2`：缺少 `out` 资质且资质窗只到 `14:00`。
- `O4` 只能签收 `feed`；`core/out` 必须由 `O1` 等覆盖该范围的签收人签署。
- `P3` 只需 `feed`，在 `12:10` 可由 `X3` 的 `shop/production` 回执放行。
- `P2`（`feed/core`）和 `P4`（`out`）在样本快照中仍被 `X4` 的 `vendor-a/maintain/core,out` 阻止；`x2` 对账状态为 `console_still_shows_vendor_control`。
- `P1` 有 `actual_start=08:33`，取消或覆盖计划会得到 `409 batch_already_started`。
- `P2` 的工艺替代设备 `D2` 匹配 `standard + feed/core`，但在收到 `D2` 的 production 回执前只可登记替代计划，不能声称许可已确认。

所有时刻均可用 `HH:MM`（按 2026-12-05 UTC+08:00 的当日分钟处理），也支持 ISO 字符串。接口默认返回 JSON；业务冲突使用 `409`，资质/范围校验使用 `422`，未找到使用 `404`。
