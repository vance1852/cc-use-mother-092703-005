# 绿色再制造与再认证平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/remanufacturing/`：退役设备（如旧变压器）回收拆解谱系、部件去向、检测与校准、修复工艺版本、再认证、校准失效批次定位与暂停；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m remanufacturing.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和旧变压器再制造全流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m remanufacturing.api --database remanufacturing.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 绿色再制造追踪与再认证（remanufacturing）

业务链条覆盖：回收设备登记（含回收重量，回应采购可见性）→ 拆解出部件并建立 `设备 → 部件` 谱系 →
部件检测（检测方法、设备、测量时有效校准）→ 修复（引用不可变工艺规范版本）→
规则允许的去向（新产品复用或报废）→ 新产品再认证 → 校准失效时定位受影响认证批次并暂停未交付产品。

角色：`intake`（回收登记）、`disassembly`（拆解/报废）、`inspector`（设备校准与检测）、
`process_engineer`（规则/工艺发布、修复、装配）、`quality`（再认证、校准失效上报、解除暂停、撤销认证）、
`auditor`（谱系/去向/证据/审计只读）。所有写接口用 `X-Actor-Id` 标识操作者。

关键不变量：

- **去向受规则约束**：每种部件类型只能进入当前生效复用规则版本允许的新产品型号，或走规则允许的报废处置
  （`material_recovery` / `hazardous_disposal` / `waste`）；部件有且仅有一个最终去向，不可重复处置。
- **证据版本固化**：复用规则与修复工艺均为版本化不可变文档（SHA-256 摘要）。签发认证时把规则版本、
  每部件检测方法/校准、修复工艺版本固化进认证证据；之后规则或工艺更新**不会追溯改写**已签发认证。
- **校准有效期门禁**：检测记录必须引用测量时刻处于有效期内、未被撤销的校准；校准失效后该设备不能再出检测。
- **失效批次定位与暂停**：上报校准失效会定位引用该设备的全部认证（含已交付，供召回追溯），
  并仅暂停尚未交付的产品；暂停期间不能交付、不能对其签发认证。解除暂停后历史可保留，产品可被再次暂停。

主要接口（端口 8083）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reuse-rules` | 发布一版不可变复用规则 |
| POST | `/instruments`、`/instruments/{id}/calibrations` | 登记检测设备 / 记录校准周期 |
| POST | `/devices`、`/devices/{id}/disassemble` | 登记回收设备 / 拆解建立谱系 |
| GET | `/devices/{id}/lineage` | 部件、检测、修复与去向谱系 |
| POST | `/components/{id}/inspections` | 记录检测（校验测量时校准） |
| POST | `/process-specs`、`/components/{id}/repairs` | 发布工艺版本 / 记录修复 |
| POST | `/components/{id}/scrap` | 规则允许的报废处置 |
| POST | `/products`、`/products/{sn}/components` | 装配新产品 / 装入合格部件（受规则约束） |
| POST | `/products/{sn}/certify`、`/products/{sn}/deliver` | 再认证 / 交付 |
| POST | `/calibration-incidents` | 上报校准失效，定位批次并暂停 |
| POST | `/products/{sn}/release-hold`、`/certificates/{n}/revoke` | 解除暂停 / 撤销认证 |
| GET | `/certificates/{n}/evidence` | 每次复用决定引用的证据版本 |
| GET | `/reports/material-destinations` | 材料最终去向汇总 |
| GET | `/audit?entity_type=&entity_id=` | 审计事件 |
