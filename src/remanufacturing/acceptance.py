"""完整再制造与再认证流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import canonical_json
from .service import RemanufacturingService
from .storage import connect, inspect_schema


def _load_demo(workspace: Path) -> dict:
    return json.loads((workspace / "fixtures" / "demo_remanufacturing.json").read_text(encoding="utf-8"))


def run(workspace: Path) -> dict[str, object]:
    demo = _load_demo(workspace)
    rules = demo["rules"]
    process_spec = demo["process_spec"]
    with tempfile.TemporaryDirectory(prefix="remanufacturing-") as temporary:
        database = Path(temporary) / "remanufacturing.sqlite3"
        connection = connect(database)
        try:
            service = RemanufacturingService(connection)
            for user_id, name, role in (
                ("intake-1", "回收登记员", "intake"),
                ("disasm-1", "拆解工程师", "disassembly"),
                ("insp-1", "检测员", "inspector"),
                ("eng-1", "工艺工程师", "process_engineer"),
                ("qa-1", "质量负责人", "quality"),
                ("auditor-1", "审计人员", "auditor"),
            ):
                service.create_user(user_id, name, role)

            rule_v1 = service.publish_reuse_rules(
                "eng-1", demo["rule_set_id"], demo["title"], rules
            )
            service.publish_process_spec(
                "eng-1", process_spec["process_code"], process_spec["title"],
                process_spec["steps"], process_spec["requirements"],
            )
            service.register_instrument("insp-1", "METER-LOSS-1", "铁芯空载损耗测试仪")
            service.record_calibration(
                "insp-1", "METER-LOSS-1", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
                "CAL-2026-0001",
            )

            # 第一台回收设备：铁芯 + 铜绕组，修复铁芯后再制造为 TR-2000 并交付。
            service.register_device(
                "intake-1", "TR-RECOV-001", "oil_immersed_transformer", "华东电网退役批次A",
                1250.0, manufacturer="某电气", model="S13-1250",
            )
            service.disassemble("disasm-1", "TR-RECOV-001", [
                {"component_id": "CORE-001", "component_type": "iron_core",
                 "material": "silicon_steel", "weight_kg": 420.0},
                {"component_id": "WIND-001", "component_type": "copper_winding",
                 "material": "electrolytic_copper", "weight_kg": 310.0},
            ])
            service.record_inspection(
                "insp-1", "CORE-001", "no_load_loss_test", "METER-LOSS-1",
                "2026-09-02T09:00:00Z", "pass",
                parameters={"voltage_kv": 10, "frequency_hz": 50}, data={"no_load_loss_w": 1200},
            )
            service.record_inspection(
                "insp-1", "WIND-001", "winding_resistance_test", "METER-LOSS-1",
                "2026-09-02T10:00:00Z", "pass",
                parameters={"temperature_c": 25}, data={"resistance_mohm": 12.4},
            )
            service.repair_component(
                "eng-1", "CORE-001", process_spec["process_code"],
                parameters={"anneal_temp_c": 380}, evidence={"furnace_batch": "F-2026-09-02"},
            )
            service.assemble_product("eng-1", "SN-TR2000-001", "TR-2000")
            service.bind_component_to_product("eng-1", "SN-TR2000-001", "CORE-001", demo["rule_set_id"])
            service.bind_component_to_product("eng-1", "SN-TR2000-001", "WIND-001", demo["rule_set_id"])
            cert1 = service.certify_product(
                "qa-1", "SN-TR2000-001", demo["rule_set_id"], "certified", "检测与修复证据齐全，准予再认证"
            )
            evidence_v1 = service.certificate_evidence("auditor-1", cert1["certificate_number"])
            service.deliver_product("qa-1", "SN-TR2000-001")

            # 规范更新：发布规则 v2 与工艺 v2，已签发认证不得被改写。
            rules_v2 = json.loads(json.dumps(rules))
            rules_v2["iron_core"]["allowed_product_models"] = ["TR-2000"]
            service.publish_reuse_rules("eng-1", demo["rule_set_id"], demo["title"] + "（修订2）", rules_v2)
            service.publish_process_spec(
                "eng-1", process_spec["process_code"], process_spec["title"] + "（修订2）",
                process_spec["steps"] + ["退火后磁性能复检"], process_spec["requirements"],
            )
            evidence_after_update = service.certificate_evidence("auditor-1", cert1["certificate_number"])

            # 第二台设备：铁芯做成尚未交付的产品；铜绕组走规则允许的报废流程。
            service.register_device(
                "intake-1", "TR-RECOV-002", "oil_immersed_transformer", "华北电网退役批次B", 900.0
            )
            service.disassemble("disasm-1", "TR-RECOV-002", [
                {"component_id": "CORE-002", "component_type": "iron_core",
                 "material": "silicon_steel", "weight_kg": 300.0},
                {"component_id": "WIND-002", "component_type": "copper_winding",
                 "material": "electrolytic_copper", "weight_kg": 210.0},
            ])
            service.record_inspection(
                "insp-1", "CORE-002", "no_load_loss_test", "METER-LOSS-1",
                "2026-09-05T09:00:00Z", "pass", data={"no_load_loss_w": 1080},
            )
            service.assemble_product("eng-1", "SN-TR2000-002", "TR-2000")
            service.bind_component_to_product("eng-1", "SN-TR2000-002", "CORE-002", demo["rule_set_id"])
            cert2 = service.certify_product(
                "qa-1", "SN-TR2000-002", demo["rule_set_id"], "certified", "第二台再认证通过"
            )
            scrap = service.scrap_component(
                "disasm-1", "WIND-002", demo["rule_set_id"], "material_recovery",
                destination="有色金属再生基地-三号炉",
            )

            # 校准失效：定位受影响认证批次，并暂停尚未交付产品。
            incident = service.report_calibration_failure(
                "qa-1", "METER-LOSS-1", "标准互感器在校准周期外，测量结果不可信"
            )
            blocked_delivery_ok = False
            try:
                service.deliver_product("qa-1", "SN-TR2000-002")
            except Exception:
                blocked_delivery_ok = True

            lineage = service.device_lineage("auditor-1", "TR-RECOV-001")
            destinations = service.material_destination_report("auditor-1")
            events = service.audit_trail("auditor-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    pinned_rule = evidence_after_update["certificate"]["rule"]
    immutable = (
        evidence_after_update["certificate"]["evidence_sha256"] == cert1["evidence_sha256"]
        and pinned_rule == f"{demo['rule_set_id']}@{rule_v1['version']}"
        and list(evidence_after_update["process_versions"]) == [f"{process_spec['process_code']}@1"]
    )
    if not immutable:
        raise RuntimeError("规范更新追溯改写了已签发认证")
    if not blocked_delivery_ok:
        raise RuntimeError("暂停产品仍可交付")
    if set(incident["affected_certificates"]) != {cert1["certificate_number"], cert2["certificate_number"]}:
        raise RuntimeError("受影响认证批次定位不正确")
    if incident["held_serials"] != ["SN-TR2000-002"] or incident["delivered_serials"] != ["SN-TR2000-001"]:
        raise RuntimeError("暂停范围不正确")
    if destinations["reused_count"] != 3 or destinations["scrapped_count"] != 1:
        raise RuntimeError("材料去向汇总不正确")

    return {
        "status": "ok",
        "certificates": [cert1["certificate_number"], cert2["certificate_number"]],
        "cert1_pinned_rule": pinned_rule,
        "cert1_evidence_sha256": cert1["evidence_sha256"],
        "immutable_after_spec_update": immutable,
        "incident": incident,
        "delivery_blocked_while_held": blocked_delivery_ok,
        "scrap_evidence_sha256": scrap["evidence_sha256"],
        "lineage_component_count": len(lineage["components"]),
        "destination_summary": {
            key: destinations[key]
            for key in ("total_components", "reused_count", "scrapped_count", "open_count",
                        "recovered_weight_kg", "settled_weight_kg")
        },
        "event_count": len(events),
        "schema": schema,
        "rule_canonical_preview": canonical_json(rules)[: 80],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行绿色再制造追踪与再认证服务的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
