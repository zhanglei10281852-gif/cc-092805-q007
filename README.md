# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、保管交接、多段运输行程、资源、预约、服务订单、墓位权属、账单和时间线。

### 多段运输行程

跨区接运（医院 → 县级殡仪站换车 → 市馆冷藏室）按 `/api/mortuary/transport-trips` 建模：行程由按序区段组成，每个区段携带起止站点、车辆与承运方、计划时间窗口、封签编号和有权确认角色。发车 `events/departed`、到达 `events/arrived`、异常停留 `events/abnormal_stop`、恢复 `events/resumed`、中途换车 `vehicle-change`、改线 `reroute` 与取消 `cancel` 都写入只追加的 `transport_events`，事件同时保留发生时间与报送时间，重复幂等键不会二次推进状态。区段必须到达并由指定角色核验封签（`confirm`）后，下一段才能发车；封签不符记为拒收。改线保留每个历史计划版本（`route_versions`），未完成区段按新版本重建，并自动重算受影响的设施预约（`impacts`）。协调员可通过 `GET /transport-trips/overview` 查看当前承运方、超时段与待确认节点，通过行程详情查看完整责任链；全部状态持久化在 SQLite，服务重启后可继续未完成行程。

## 测试与编译检查

测试命令：python -m pytest

编译命令：python -m compileall -q app tests

API 与 CLI 冒烟命令：python -m app.cli smoke、python -m app.cli mortuary-demo

## 目录结构

- app/mortuary：档案、保管交接、资源排程、权属和账单领域
- app/api：登录、角色、审计及系统管理接口
- app/core：时钟、安全、异常、隐私与分页能力
- app/repositories：通用身份和审计数据访问
- app/services：会话、权限、后台任务及维护服务
- tests：领域、接口、异常路径和身份回归测试

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。
