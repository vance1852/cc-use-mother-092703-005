# 绿色再制造与再认证平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入，并为回收设备建立从回收到再认证、复用/报废的完整追踪谱系。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/remanufacture/`：回收设备拆解谱系、部件检测/修复、版本化工艺规范与复用规则、再认证证据快照、校准失效影响定位与材料最终去向；
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
PYTHONPATH=src python3 -m remanufacture.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和变压器再制造全流程，不访问外部网络。

## 再制造追踪服务（remanufacture）

### 业务不变量

1. **拆解谱系**：回收设备（回收重量对采购可见）→ 一次拆解 → 部件清单；部件重量与拆解残余之和必须等于回收重量，设备不可重复拆解。
2. **部件状态机**：`recovered → inspected → repaired → qualified → reused/scrapped`，只允许合法单向推进。
3. **检测与校准**：检测必须记录方法、判定准则、实测值与所用检测设备，且检测时间落在设备校准有效期内；检测记录不可变。
4. **版本化规范/规则**：工艺规范与复用规则只追加新版本（内容哈希变化才发版），修复记录钉住 `spec_id@version + sha256`。
5. **证据快照不被追溯改写**：再认证签发时把检测、修复、规则版本与摘要整体快照（`evidence_sha256`）；之后规范或规则升版不影响已签发认证。
6. **去向受限且唯一**：每个部件只能有一条终态去向——进入规则允许的新产品类型，或在规则允许时报废；去向记录同样钉住规则证据版本。
7. **校准失效处置**：报告失效后，按时间窗 `[invalid_from, detected_at)` 定位嫌疑检测 → 受影响认证批次 → 暂停批次下**尚未交付**的产品；已交付产品只统计不改动。复核通过可解除暂停，但认证证据快照保持不变。
8. **管理查询**：材料最终去向报告（复用产品 / 报废目的地 / 拆解残余 / 重量汇总）与部件复用决定的证据版本链（规则版本、认证证据摘要、检测与修复依据）。

### 角色

`intake`（回收登记）、`dismantler`（拆解）、`inspector`（检测设备与检测）、`engineer`（规范/规则/修复/生产）、`certifier`（再认证签发）、`logistics`（报废与交付）、`auditor`（报告与审计链）。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/devices` | 登记回收设备 |
| GET | `/devices/{id}/genealogy` | 拆解谱系与部件去向 |
| POST | `/disassemblies` | 登记拆解（重量平衡校验） |
| POST | `/equipment`、`/equipment/{id}/recalibrate` | 检测设备登记与重新校准 |
| POST | `/inspections` | 登记部件检测（校准窗口校验） |
| POST | `/process-specs` | 发布工艺规范新版本 |
| POST | `/reuse-policies` | 发布复用规则新版本 |
| POST | `/repairs` | 登记修复（钉住规范版本） |
| POST | `/certifications` | 签发再认证（证据快照） |
| POST | `/products`、`/products/{id}/assign`、`/products/{id}/deliver` | 新产品、部件去向、交付 |
| POST | `/scrap` | 部件报废（规则许可校验） |
| POST | `/calibration-incidents`、`/calibration-incidents/{id}/resolve` | 校准失效定位与暂停/解除 |
| GET | `/reports/material-destination?device_id=` | 材料最终去向与重量汇总 |
| GET | `/components/{id}/reuse-evidence` | 复用决定引用的证据版本 |
| GET | `/audit/chain` | 哈希链审计校验 |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m remanufacture.api --database remanufacture.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON（写接口需 `X-Actor-Id` 头）。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
