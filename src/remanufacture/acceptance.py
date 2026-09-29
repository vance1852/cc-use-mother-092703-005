"""贯通回收、拆解、检测、修复、再认证、去向与校准失效处置的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RemanufactureService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = RemanufactureService(
        connection, FrozenClock(datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc))
    )

    users = (
        ("intake", "intake", "回收登记员"),
        ("dismantler", "dismantler", "拆解工程师"),
        ("inspector", "inspector", "检测员"),
        ("engineer", "engineer", "工艺工程师"),
        ("certifier", "certifier", "质量负责人"),
        ("logistics", "logistics", "物流员"),
        ("auditor", "auditor", "审计员"),
    )
    for user_id, role, name in users:
        service.create_user(user_id, name, role)

    # 1. 采购部门登记回收设备与回收重量
    service.register_device("intake", {
        "device_id": "TR-OLD-001",
        "device_type": "oil-transformer",
        "model": "S13-1000/10",
        "serial_no": "S-2008-0771",
        "source": "华东数据中心退运",
        "recovered_weight_kg": "2400",
        "recovered_at": "2026-09-28T02:00:00Z",
    })

    # 2. 拆解团队建立谱系：重量必须与回收重量平衡
    genealogy = service.disassemble(
        "dismantler", "dis-001", "TR-OLD-001", "2026-09-29T03:00:00Z",
        components=[
            {"component_id": "core-a", "component_type": "iron_core", "name": "铁芯A相", "material": "硅钢片", "weight_kg": "600"},
            {"component_id": "core-b", "component_type": "iron_core", "name": "铁芯B相", "material": "硅钢片", "weight_kg": "600"},
            {"component_id": "winding-a", "component_type": "copper_winding", "name": "铜绕组A套", "material": "电解铜", "weight_kg": "400"},
            {"component_id": "winding-b", "component_type": "copper_winding", "name": "铜绕组B套", "material": "电解铜", "weight_kg": "400"},
            {"component_id": "tank-001", "component_type": "steel_tank", "name": "钢制油箱", "material": "Q235钢", "weight_kg": "350"},
        ],
        residue_weight_kg="50",
        residue_destination="保温纸与油污按危废处置",
        note="三相铁芯拆为两套复用部件",
    )

    # 3. 检测设备与校准有效期
    service.register_equipment("inspector", {
        "equipment_id": "eq-turns", "name": "变比组别测试仪", "serial_no": "TRT-12",
        "calibration_valid_from": "2026-01-01T00:00:00Z", "calibration_valid_to": "2026-12-31T23:59:59Z",
        "certificate_ref": "CAL-2026-0001",
    })
    service.register_equipment("inspector", {
        "equipment_id": "eq-ohm", "name": "绕组直流电阻测试仪", "serial_no": "DCR-08",
        "calibration_valid_from": "2026-01-01T00:00:00Z", "calibration_valid_to": "2026-12-31T23:59:59Z",
        "certificate_ref": "CAL-2026-0002",
    })

    # 4. 检测：铁芯合格；A 套铜绕组初测不合格 -> 修复 -> 复测合格；油箱不合格走报废
    for core in ("core-a", "core-b"):
        service.record_inspection("inspector", {
            "inspection_id": f"insp-{core}", "component_id": core,
            "method_code": "GB1094.3-TURNS", "method_name": "变比与联结组标号检测",
            "equipment_id": "eq-turns", "inspected_at": "2026-10-01T06:00:00Z", "result": "pass",
            "measured": {"ratio_error_percent": "0.21", "winding_resistance_ohm": "0.92"},
            "criteria": {"ratio_error_percent_max": "0.5"},
        })
    service.record_inspection("inspector", {
        "inspection_id": "insp-winding-a-1", "component_id": "winding-a",
        "method_code": "GB1094.3-DCR", "method_name": "绕组直流电阻检测",
        "equipment_id": "eq-ohm", "inspected_at": "2026-10-01T08:00:00Z", "result": "fail",
        "measured": {"resistance_unbalance_percent": "3.2"},
        "criteria": {"resistance_unbalance_percent_max": "2.0"},
    })
    service.record_inspection("inspector", {
        "inspection_id": "insp-tank", "component_id": "tank-001",
        "method_code": "VT-PRESSURE", "method_name": "油箱承压目视检测",
        "equipment_id": "eq-ohm", "inspected_at": "2026-10-01T09:00:00Z", "result": "fail",
        "measured": {"crack_count": 2},
        "criteria": {"crack_count_max": 0},
    })

    # 5. 工艺规范 v1 与复用规则 v1
    service.publish_process_spec("engineer", {
        "spec_id": "spec-winding-rework", "title": "铜绕组整理与重焊工艺",
        "content": {"cleaning": "超声除油", "braze_temp_c": 680, "insulation_class": "H",
                    "reference": "QP-RM-014 v1"},
    })
    service.publish_reuse_policy("engineer", {
        "policy_id": "policy-transformer", "title": "变压器部件再制造复用规则",
        "rules": {
            "iron_core": {"allow_new_products": ["distribution-transformer"], "allow_scrap": False},
            "copper_winding": {"allow_new_products": ["distribution-transformer", "industrial-transformer"],
                               "allow_scrap": False},
            "steel_tank": {"allow_new_products": [], "allow_scrap": True},
        },
    })

    # 6. 按 v1 工艺修复 A 套绕组并复测
    service.repair_component("engineer", {
        "repair_id": "rep-winding-a", "component_id": "winding-a",
        "spec_id": "spec-winding-rework", "spec_version": 1,
        "parameters": {"joints_rebrazed": 2, "actual_braze_temp_c": 684},
        "repaired_at": "2026-10-03T07:30:00Z",
    })
    service.record_inspection("inspector", {
        "inspection_id": "insp-winding-a-2", "component_id": "winding-a",
        "method_code": "GB1094.3-DCR", "method_name": "绕组直流电阻检测",
        "equipment_id": "eq-ohm", "inspected_at": "2026-10-04T06:00:00Z", "result": "pass",
        "measured": {"resistance_unbalance_percent": "1.1"},
        "criteria": {"resistance_unbalance_percent_max": "2.0"},
    })
    service.record_inspection("inspector", {
        "inspection_id": "insp-winding-b", "component_id": "winding-b",
        "method_code": "GB1094.3-DCR", "method_name": "绕组直流电阻检测",
        "equipment_id": "eq-ohm", "inspected_at": "2026-10-04T08:00:00Z", "result": "pass",
        "measured": {"resistance_unbalance_percent": "0.8"},
        "criteria": {"resistance_unbalance_percent_max": "2.0"},
    })

    # 7. 质量负责人签发再认证（证据整体快照）
    certification = service.issue_certification("certifier", {
        "certification_id": "cert-001", "batch_no": "B-2026-10-001",
        "product_type": "distribution-transformer",
        "component_ids": ["core-a", "core-b", "winding-a", "winding-b"],
        "policy_id": "policy-transformer",
        "note": "全部部件按现行规则与检测方法合格",
    })
    evidence_sha_before = certification["evidence_sha256"]

    # 8. 装成两台新产品并分配部件去向（规则只允许 distribution-transformer）
    service.build_product("engineer", {
        "product_id": "RX-100", "certification_id": "cert-001", "built_at": "2026-10-06T01:00:00Z"})
    service.build_product("engineer", {
        "product_id": "RX-101", "certification_id": "cert-001", "built_at": "2026-10-08T01:00:00Z"})
    service.assign_to_product("engineer", "RX-100", ["core-a", "winding-a"], idempotency_key="assign-100")
    service.assign_to_product("engineer", "RX-101", ["core-b", "winding-b"])
    service.deliver_product("logistics", "RX-100", "2026-10-10T02:00:00Z", "某市供电公司")

    # 油箱只能报废，不能进入新产品
    scrap = service.scrap_component(
        "logistics", "disp-tank-001", "tank-001", "华东金属回收公司-废钢堆场",
        "policy-transformer", note="油箱裂纹不可修复")

    # 9. 工艺规范升级 v2、复用规则升级 v2：已签发认证证据不变
    service.publish_process_spec("engineer", {
        "spec_id": "spec-winding-rework", "title": "铜绕组整理与重焊工艺",
        "content": {"cleaning": "超声除油", "braze_temp_c": 660, "insulation_class": "H",
                    "reference": "QP-RM-014 v2"},
    })
    service.publish_reuse_policy("engineer", {
        "policy_id": "policy-transformer", "title": "变压器部件再制造复用规则",
        "rules": {
            "iron_core": {"allow_new_products": ["distribution-transformer", "industrial-transformer"],
                          "allow_scrap": False},
            "copper_winding": {"allow_new_products": ["distribution-transformer", "industrial-transformer"],
                               "allow_scrap": False},
            "steel_tank": {"allow_new_products": [], "allow_scrap": True},
        },
    })
    cert_after_policy_update = service.certification("cert-001")

    # 10. 发现变比测试仪自 8 月起校准失效：定位认证批次，暂停未交付产品
    incident = service.report_calibration_incident(
        "inspector", "inc-2026-007", "eq-turns",
        invalid_from="2026-08-01T00:00:00Z",
        reason="实验室比对发现量程超差",
        detected_at="2026-10-20T08:00:00Z",
    )
    product_100 = service.product("RX-100")
    product_101 = service.product("RX-101")

    # 11. 管理接口：材料最终去向 + 复用决定引用的证据版本
    destination_report = service.material_destination_report("auditor", "TR-OLD-001")
    reuse_evidence = service.reuse_decision_evidence("auditor", "core-a")

    # 12. 复核完成（重新校准合格），解除暂停；认证证据快照仍未改变
    service.recalibrate("inspector", "eq-turns", "2026-10-21T01:00:00Z",
                        "2026-10-21T00:00:00Z", "2027-10-20T23:59:59Z", "CAL-2027-0009")
    resolved = service.resolve_calibration_incident(
        "inspector", "inc-2026-007", "期间产品变比复测全部合格", release=True)
    cert_final = service.certification("cert-001")

    connection.close()
    return {
        "status": "ok",
        "workspace": workspace.name,
        "components": len(genealogy["components"]),
        "certification": {
            "certification_id": certification["certification_id"],
            "batch_no": certification["batch_no"],
            "state": cert_final["state"],
            "policy_version_in_evidence": certification["evidence"]["policy"]["version"],
            "policy_sha256_in_evidence": certification["policy_sha256"],
            "evidence_sha256": evidence_sha_before,
            "evidence_unchanged_after_updates": cert_after_policy_update["evidence_sha256"] == evidence_sha_before,
            "evidence_unchanged_after_incident": cert_final["evidence_sha256"] == evidence_sha_before,
            "spec_version_used_by_winding_repair": "1",
        },
        "scrap": scrap,
        "incident": {
            "incident_id": incident["incident_id"],
            "suspect_inspections": incident["affected_inspection_count"],
            "affected_certifications": incident["affected_certification_count"],
            "suspended_products": incident["suspended_product_count"],
            "delivered_product_rx100_state_during_incident": product_100["state"],
            "undelivered_product_rx101_state_during_incident": product_101["state"],
            "resolved_state": resolved["state"],
        },
        "material_weight_summary_kg": destination_report["weight_summary_kg"],
        "core_a_destination": next(
            item for item in destination_report["components"] if item["component_id"] == "core-a"
        )["destination"],
        "core_a_evidence_policy_version": reuse_evidence["policy"]["version"],
        "core_a_evidence_certification_sha256": reuse_evidence["certification"]["evidence_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行绿色再制造追踪服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
