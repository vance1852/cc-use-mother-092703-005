from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from remanufacture.clock import FrozenClock
from remanufacture.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from remanufacture.service import RemanufactureService


def fixture_service() -> tuple[sqlite3.Connection, RemanufactureService]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = RemanufactureService(
        connection, FrozenClock(datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc))
    )
    for user_id, role in (
        ("intake", "intake"),
        ("dismantler", "dismantler"),
        ("inspector", "inspector"),
        ("engineer", "engineer"),
        ("certifier", "certifier"),
        ("logistics", "logistics"),
        ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    return connection, service


POLICY_RULES = {
    "iron_core": {"allow_new_products": ["distribution-transformer"], "allow_scrap": False},
    "copper_winding": {"allow_new_products": ["distribution-transformer", "industrial-transformer"],
                       "allow_scrap": False},
    "steel_tank": {"allow_new_products": [], "allow_scrap": True},
}


class RemanufactureFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection, self.service = fixture_service()

    def tearDown(self) -> None:
        self.connection.close()

    def _seed_device_with_core(self) -> None:
        self.service.register_device("intake", {
            "device_id": "D1", "device_type": "oil-transformer", "model": "S13",
            "serial_no": "SN-1", "source": "退运", "recovered_weight_kg": "1000",
            "recovered_at": "2026-09-28T00:00:00Z",
        })
        self.service.disassemble("dismantler", "DIS1", "D1", "2026-09-29T00:00:00Z", [
            {"component_id": "core1", "component_type": "iron_core", "name": "铁芯", "weight_kg": "600"},
            {"component_id": "tank1", "component_type": "steel_tank", "name": "油箱", "weight_kg": "350"},
        ], residue_weight_kg="50")

    def _seed_equipment_and_inspections(self) -> None:
        self.service.register_equipment("inspector", {
            "equipment_id": "EQ1", "name": "变比仪", "serial_no": "X1",
            "calibration_valid_from": "2026-01-01T00:00:00Z",
            "calibration_valid_to": "2026-12-31T23:59:59Z"})
        self.service.record_inspection("inspector", {
            "inspection_id": "I1", "component_id": "core1",
            "method_code": "M1", "method_name": "变比检测", "equipment_id": "EQ1",
            "inspected_at": "2026-10-01T00:00:00Z", "result": "pass",
            "measured": {"ratio_error_percent": "0.2"}, "criteria": {"max": "0.5"}})
        self.service.record_inspection("inspector", {
            "inspection_id": "I2", "component_id": "tank1",
            "method_code": "M2", "method_name": "承压检测", "equipment_id": "EQ1",
            "inspected_at": "2026-10-01T01:00:00Z", "result": "fail",
            "measured": {"cracks": 2}, "criteria": {"max": 0}})

    def _seed_policy_and_cert(self) -> dict:
        self.service.publish_reuse_policy("engineer", {
            "policy_id": "P1", "title": "复用规则", "rules": POLICY_RULES})
        return self.service.issue_certification("certifier", {
            "certification_id": "C1", "batch_no": "B1",
            "product_type": "distribution-transformer",
            "component_ids": ["core1"], "policy_id": "P1"})

    def test_weight_balance_enforced_on_disassembly(self) -> None:
        self.service.register_device("intake", {
            "device_id": "D2", "device_type": "oil-transformer", "model": "S13",
            "serial_no": "SN-2", "source": "退运", "recovered_weight_kg": "1000",
            "recovered_at": "2026-09-28T00:00:00Z"})
        with self.assertRaises(Conflict):
            self.service.disassemble("dismantler", "DIS2", "D2", "2026-09-29T00:00:00Z", [
                {"component_id": "c", "component_type": "iron_core", "name": "铁芯", "weight_kg": "900"}])

    def test_genealogy_tracks_components_and_state(self) -> None:
        self._seed_device_with_core()
        genealogy = self.service.genealogy("D1")
        self.assertEqual(genealogy["device"]["state"], "dismantled")
        self.assertEqual([c["component_id"] for c in genealogy["components"]], ["core1", "tank1"])
        with self.assertRaises(InvalidState):
            self.service.disassemble("dismantler", "DIS1B", "D1", "2026-09-30T00:00:00Z", [
                {"component_id": "c2", "component_type": "iron_core", "name": "x", "weight_kg": "1000"}])

    def test_inspection_requires_valid_calibration_window(self) -> None:
        self._seed_device_with_core()
        self.service.register_equipment("inspector", {
            "equipment_id": "EQ-EXPIRED", "name": "旧仪器", "serial_no": "X2",
            "calibration_valid_from": "2025-01-01T00:00:00Z",
            "calibration_valid_to": "2025-12-31T23:59:59Z"})
        with self.assertRaises(InvalidState):
            self.service.record_inspection("inspector", {
                "inspection_id": "IX", "component_id": "core1",
                "method_code": "M1", "method_name": "变比", "equipment_id": "EQ-EXPIRED",
                "inspected_at": "2026-10-01T00:00:00Z", "result": "pass"})

    def test_repair_pins_spec_version(self) -> None:
        self._seed_device_with_core()
        self._seed_equipment_and_inspections()
        self.service.publish_process_spec("engineer", {
            "spec_id": "S1", "title": "绕组工艺",
            "content": {"braze_temp_c": 680, "step": "v1"}})
        # tank1 检测为 fail 后处于 inspected 状态，但修复工艺适用于绕组；这里用 core1 演示版本钉住
        self.service.repair_component("engineer", {
            "repair_id": "R1", "component_id": "core1", "spec_id": "S1", "spec_version": 1,
            "parameters": {"note": "整理"}, "repaired_at": "2026-10-02T00:00:00Z"})
        self.service.publish_process_spec("engineer", {
            "spec_id": "S1", "title": "绕组工艺",
            "content": {"braze_temp_c": 660, "step": "v2"}})
        repair = self.service.repair("R1")
        self.assertEqual(repair["spec_version"], 1)
        self.assertEqual(self.service.process_spec("S1", 1)["content"]["braze_temp_c"], 680)
        self.assertEqual(self.service.process_spec("S1")["version"], 2)
        with self.assertRaises(Conflict):
            self.service.publish_process_spec("engineer", {
                "spec_id": "S1", "title": "绕组工艺",
                "content": {"braze_temp_c": 660, "step": "v2"}})

    def test_policy_forbids_disallowed_destination(self) -> None:
        self._seed_device_with_core()
        self._seed_equipment_and_inspections()
        self.service.publish_reuse_policy("engineer", {
            "policy_id": "P1", "title": "复用规则", "rules": POLICY_RULES})
        # 铁芯禁止报废
        with self.assertRaises(Conflict):
            self.service.scrap_component("logistics", "DP1", "core1", "堆场", "P1")
        # 铁芯规则只允许 distribution-transformer，不能进入 industrial-transformer
        with self.assertRaises(Conflict):
            self.service.issue_certification("certifier", {
                "certification_id": "CX", "batch_no": "BX",
                "product_type": "industrial-transformer",
                "component_ids": ["core1"], "policy_id": "P1"})

    def test_certification_requires_passing_inspection_and_pins_evidence(self) -> None:
        self._seed_device_with_core()
        self._seed_equipment_and_inspections()
        cert = self._seed_policy_and_cert()
        evidence_sha = cert["evidence_sha256"]
        self.assertEqual(cert["evidence"]["policy"]["version"], 1)
        # 规则升级 v2（放开铁芯进入 industrial-transformer）
        new_rules = dict(POLICY_RULES)
        new_rules["iron_core"] = {"allow_new_products": ["distribution-transformer", "industrial-transformer"],
                                  "allow_scrap": False}
        self.service.publish_reuse_policy("engineer", {
            "policy_id": "P1", "title": "复用规则v2", "rules": new_rules})
        refreshed = self.service.certification("C1")
        self.assertEqual(refreshed["evidence_sha256"], evidence_sha)
        self.assertEqual(refreshed["policy_version"], 1)
        self.assertEqual(self.service.reuse_policy("P1")["version"], 2)
        # 部件不能重复认证
        with self.assertRaises(Conflict):
            self.service.issue_certification("certifier", {
                "certification_id": "C2", "batch_no": "B2",
                "product_type": "distribution-transformer",
                "component_ids": ["core1"], "policy_id": "P1"})

    def test_full_disposition_flow_and_evidence_report(self) -> None:
        self._seed_device_with_core()
        self._seed_equipment_and_inspections()
        self._seed_policy_and_cert()
        self.service.build_product("engineer", {
            "product_id": "P-100", "certification_id": "C1", "built_at": "2026-10-06T00:00:00Z"})
        assigned = self.service.assign_to_product("engineer", "P-100", ["core1"], "key-1")
        self.assertEqual(assigned["count"], 1)
        # 幂等重放
        replayed = self.service.assign_to_product("engineer", "P-100", ["core1"], "key-1")
        self.assertEqual(replayed, assigned)
        # 油箱报废
        scrap = self.service.scrap_component(
            "logistics", "DP-TANK", "tank1", "废钢堆场", "P1", note="裂纹")
        self.assertEqual(scrap["kind"], "scrap")
        # 不能再次去向
        with self.assertRaises(Conflict):
            self.service.scrap_component("logistics", "DP-TANK2", "tank1", "别处", "P1")
        report = self.service.material_destination_report("auditor", "D1")
        self.assertEqual(report["weight_summary_kg"]["new_product"], "600")
        self.assertEqual(report["weight_summary_kg"]["scrap"], "350")
        self.assertEqual(report["weight_summary_kg"]["residue"], "50")
        self.assertEqual(report["weight_summary_kg"]["undisposed"], "0")
        evidence = self.service.reuse_decision_evidence("auditor", "core1")
        self.assertEqual(evidence["policy"]["version"], 1)
        self.assertEqual(evidence["certification"]["batch_no"], "B1")
        self.assertEqual(evidence["certification_evidence"]["inspection"]["inspection_id"], "I1")

    def test_calibration_incident_suspends_undelivered_only(self) -> None:
        self._seed_device_with_core()
        self._seed_equipment_and_inspections()
        self._seed_policy_and_cert()
        self.service.build_product("engineer", {
            "product_id": "PA", "certification_id": "C1", "built_at": "2026-10-06T00:00:00Z"})
        self.service.assign_to_product("engineer", "PA", ["core1"])
        self.service.deliver_product("logistics", "PA", "2026-10-10T00:00:00Z", "客户甲")
        # 认证已无部件可装第二台产品的完整批次，但仍可注册空产品验证暂停逻辑：
        # 改为再建一台未交付产品（认证批次允许多台产品，部件去向各自独立）
        self.service.build_product("engineer", {
            "product_id": "PB", "certification_id": "C1", "built_at": "2026-10-09T00:00:00Z"})
        incident = self.service.report_calibration_incident(
            "inspector", "INC1", "EQ1", "2026-09-01T00:00:00Z",
            "量程超差", detected_at="2026-10-20T08:00:00Z")
        self.assertEqual(incident["affected_inspection_count"], 2)
        self.assertEqual(incident["affected_certification_count"], 1)
        self.assertEqual(incident["suspended_product_count"], 1)
        impact = incident["impacts"][0]
        self.assertEqual(impact["delivered_product_count"], 1)
        self.assertEqual(self.service.product("PA")["state"], "delivered")
        self.assertEqual(self.service.product("PB")["state"], "suspended")
        self.assertEqual(self.service.certification("C1")["state"], "suspended")
        # 暂停期间不能交付
        with self.assertRaises(InvalidState):
            self.service.deliver_product("logistics", "PB", "2026-10-21T00:00:00Z", "客户乙")
        # 证据快照不变
        cert = self.service.certification("C1")
        self.assertEqual(cert["evidence"]["policy"]["version"], 1)
        # 检测记录被标记嫌疑但内容保留
        self.assertEqual(self.service.inspection("I1")["state"], "suspect")
        # 复核解除
        resolved = self.service.resolve_calibration_incident(
            "inspector", "INC1", "复测合格", release=True)
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(self.service.product("PB")["state"], "built")
        self.assertEqual(self.service.certification("C1")["state"], "issued")
        self.assertEqual(self.service.inspection("I1")["state"], "valid")

    def test_incident_window_excludes_early_and_late_inspections(self) -> None:
        self._seed_device_with_core()
        self.service.register_equipment("inspector", {
            "equipment_id": "EQ2", "name": "电阻仪", "serial_no": "X3",
            "calibration_valid_from": "2026-01-01T00:00:00Z",
            "calibration_valid_to": "2026-12-31T23:59:59Z"})
        self.service.record_inspection("inspector", {
            "inspection_id": "EARLY", "component_id": "core1",
            "method_code": "M", "method_name": "检测", "equipment_id": "EQ2",
            "inspected_at": "2026-08-01T00:00:00Z", "result": "pass"})
        incident = self.service.report_calibration_incident(
            "inspector", "INC2", "EQ2", "2026-09-01T00:00:00Z",
            "超差", detected_at="2026-10-01T00:00:00Z")
        self.assertEqual(incident["affected_inspection_count"], 0)
        self.assertEqual(incident["affected_certification_count"], 0)

    def test_two_open_incidents_keep_cert_suspended_until_both_resolved(self) -> None:
        # 同一认证批次的两个部件分别由两台设备检测
        self.service.register_device("intake", {
            "device_id": "D3", "device_type": "oil-transformer", "model": "S13",
            "serial_no": "SN-3", "source": "退运", "recovered_weight_kg": "200",
            "recovered_at": "2026-09-28T00:00:00Z"})
        self.service.disassemble("dismantler", "DIS3", "D3", "2026-09-29T00:00:00Z", [
            {"component_id": "core1", "component_type": "iron_core", "name": "铁芯1", "weight_kg": "100"},
            {"component_id": "core2", "component_type": "iron_core", "name": "铁芯2", "weight_kg": "100"},
        ])
        for equipment_id, serial in (("EQ1", "X1"), ("EQ2", "X2")):
            self.service.register_equipment("inspector", {
                "equipment_id": equipment_id, "name": "仪器", "serial_no": serial,
                "calibration_valid_from": "2026-01-01T00:00:00Z",
                "calibration_valid_to": "2026-12-31T23:59:59Z"})
        for component_id, equipment_id in (("core1", "EQ1"), ("core2", "EQ2")):
            self.service.record_inspection("inspector", {
                "inspection_id": f"I-{component_id}", "component_id": component_id,
                "method_code": "M1", "method_name": "变比", "equipment_id": equipment_id,
                "inspected_at": "2026-10-01T00:00:00Z", "result": "pass"})
        self.service.publish_reuse_policy("engineer", {
            "policy_id": "P1", "title": "复用规则", "rules": {
                "iron_core": {"allow_new_products": ["distribution-transformer"], "allow_scrap": False}}})
        self.service.issue_certification("certifier", {
            "certification_id": "C1", "batch_no": "B1",
            "product_type": "distribution-transformer",
            "component_ids": ["core1", "core2"], "policy_id": "P1"})
        self.service.build_product("engineer", {
            "product_id": "PB", "certification_id": "C1", "built_at": "2026-10-06T00:00:00Z"})
        self.service.report_calibration_incident(
            "inspector", "INC1", "EQ1", "2026-09-01T00:00:00Z",
            "超差", detected_at="2026-10-20T08:00:00Z")
        self.service.report_calibration_incident(
            "inspector", "INC2", "EQ2", "2026-09-01T00:00:00Z",
            "超差", detected_at="2026-10-20T09:00:00Z")
        self.assertEqual(self.service.certification("C1")["state"], "suspended")
        # 只关闭第一个事件：仍有未关闭事件，维持暂停
        self.service.resolve_calibration_incident("inspector", "INC1", "复测合格", release=True)
        self.assertEqual(self.service.certification("C1")["state"], "suspended")
        self.assertEqual(self.service.product("PB")["state"], "suspended")
        # 第二个事件关闭后才恢复
        self.service.resolve_calibration_incident("inspector", "INC2", "复测合格", release=True)
        self.assertEqual(self.service.certification("C1")["state"], "issued")
        self.assertEqual(self.service.product("PB")["state"], "built")

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_device("auditor", {
                "device_id": "D9", "device_type": "t", "model": "m", "serial_no": "s",
                "source": "x", "recovered_weight_kg": "1", "recovered_at": "2026-09-28T00:00:00Z"})
        with self.assertRaises(Forbidden):
            self.service.publish_reuse_policy("inspector", {
                "policy_id": "P", "title": "t", "rules": POLICY_RULES})

    def test_validation_missing_fields(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_device("intake", {"device_id": "D"})

    def test_audit_chain(self) -> None:
        self._seed_device_with_core()
        chain = self.service.audit_chain("auditor")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)
        with self.assertRaises(Forbidden):
            self.service.audit_chain("intake")


if __name__ == "__main__":
    unittest.main()
