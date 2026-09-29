from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from remanufacturing.clock import FrozenClock
from remanufacturing.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from remanufacturing.service import RemanufacturingService


def make_service() -> tuple[sqlite3.Connection, RemanufacturingService]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
    return connection, RemanufacturingService(connection, clock)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection, self.service = make_service()
        for user_id, name, role in (
            ("intake", "回收员", "intake"),
            ("disasm", "拆解员", "disassembly"),
            ("insp", "检测员", "inspector"),
            ("eng", "工艺工程师", "process_engineer"),
            ("qa", "质量负责人", "quality"),
            ("aud", "审计员", "auditor"),
        ):
            self.service.create_user(user_id, name, role)
        self.service.publish_reuse_rules("eng", "R1", "规则v1", {
            "iron_core": {"allowed_product_models": ["TR-1000", "TR-2000"],
                          "scrap_dispositions": ["material_recovery"]},
            "copper_winding": {"allowed_product_models": ["TR-2000"],
                               "scrap_dispositions": ["material_recovery", "hazardous_disposal"]},
            "gasket": {"allowed_product_models": [], "scrap_dispositions": ["waste"]},
        })
        self.service.publish_process_spec("eng", "PROC", "铁芯工艺", ["清理", "退火"])
        self.service.register_instrument("insp", "M1", "损耗仪")
        self.service.record_calibration(
            "insp", "M1", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z", "CERT-1"
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _device(self, device_id: str = "D1", components=("CORE1", "WIND1")) -> None:
        self.service.register_device("intake", device_id, "transformer", "退役批次", 1000.0)
        types = {"CORE1": "iron_core", "WIND1": "copper_winding", "GASKET1": "gasket",
                 "CORE2": "iron_core"}
        self.service.disassemble("disasm", device_id, [
            {"component_id": cid, "component_type": types[cid], "weight_kg": 100.0}
            for cid in components
        ])

    def _inspect_pass(self, component_id: str, at: str = "2026-09-02T09:00:00Z") -> None:
        self.service.record_inspection(
            "insp", component_id, "loss_test", "M1", at, "pass", data={"loss_w": 1000}
        )

    def _certified_product(self, serial: str, model: str = "TR-2000",
                           components=("CORE1", "WIND1")) -> str:
        self.service.assemble_product("eng", serial, model)
        for component_id in components:
            self.service.bind_component_to_product("eng", serial, component_id, "R1")
        result = self.service.certify_product("qa", serial, "R1", "certified", "合格")
        return result["certificate_number"]

    def test_full_chain_lineage_and_destination(self) -> None:
        self._device()
        self._inspect_pass("CORE1")
        self._inspect_pass("WIND1")
        self.service.repair_component("eng", "CORE1", "PROC", parameters={"temp": 380})
        certificate = self._certified_product("SN1")
        lineage = self.service.device_lineage("aud", "D1")
        self.assertEqual({c["component_id"] for c in lineage["components"]}, {"CORE1", "WIND1"})
        core = next(c for c in lineage["components"] if c["component_id"] == "CORE1")
        self.assertEqual(core["disposition"]["kind"], "reuse")
        self.assertEqual(core["disposition"]["serial_number"], "SN1")
        self.assertEqual(len(core["inspections"]), 1)
        self.assertEqual(core["repairs"][0]["process_code"], "PROC")
        evidence = self.service.certificate_evidence("aud", certificate)
        self.assertEqual(evidence["certificate"]["rule"], "R1@1")
        self.assertEqual(len(evidence["certificate"]["evidence_sha256"]), 64)
        self.assertEqual(set(evidence["process_versions"]), {"PROC@1"})

    def test_component_cannot_enter_product_model_disallowed_by_rules(self) -> None:
        self._device()
        self._inspect_pass("CORE1")
        self._inspect_pass("WIND1")
        self.service.assemble_product("eng", "SN-BAD", "TR-1000")
        # 铜绕组按规则不能进入 TR-1000。
        with self.assertRaises(InvalidState):
            self.service.bind_component_to_product("eng", "SN-BAD", "WIND1", "R1")
        # 铁芯可以装入，但认证时整机型式仍受规则约束（此处铁芯允许，故可通过）。
        self.service.bind_component_to_product("eng", "SN-BAD", "CORE1", "R1")

    def test_scrap_disposition_must_be_allowed(self) -> None:
        self._device(components=("GASKET1",))
        self._inspect_pass("GASKET1")
        with self.assertRaises(InvalidState):
            self.service.scrap_component("disasm", "GASKET1", "R1", "material_recovery")
        result = self.service.scrap_component("disasm", "GASKET1", "R1", "waste",
                                              destination="危废暂存间")
        self.assertEqual(result["kind"], "scrap")
        report = self.service.material_destination_report("aud")
        self.assertEqual(report["scrapped_count"], 1)
        self.assertEqual(report["reused_count"], 0)

    def test_spec_updates_do_not_rewrite_issued_certificate(self) -> None:
        self._device()
        self._inspect_pass("CORE1")
        self._inspect_pass("WIND1")
        self.service.repair_component("eng", "CORE1", "PROC")
        certificate = self._certified_product("SN1")
        before = self.service.certificate_evidence("aud", certificate)

        # 发布规则 v2（移除 TR-1000）和工艺 v2。
        self.service.publish_reuse_rules("eng", "R1", "规则v2", {
            "iron_core": {"allowed_product_models": ["TR-2000"],
                          "scrap_dispositions": ["material_recovery"]},
            "copper_winding": {"allowed_product_models": ["TR-2000"],
                               "scrap_dispositions": ["material_recovery"]},
            "gasket": {"allowed_product_models": [], "scrap_dispositions": ["waste"]},
        })
        self.service.publish_process_spec("eng", "PROC", "铁芯工艺v2", ["清理", "退火", "复检"])
        after = self.service.certificate_evidence("aud", certificate)
        self.assertEqual(after["certificate"]["rule"], "R1@1")
        self.assertEqual(after["certificate"]["evidence_sha256"],
                         before["certificate"]["evidence_sha256"])
        self.assertEqual(set(after["process_versions"]), {"PROC@1"})
        self.assertEqual(after["rule_version"]["content_sha256"],
                         before["rule_version"]["content_sha256"])

    def test_new_rule_version_governs_new_decisions(self) -> None:
        # 规则 v2 不再允许铁芯进入 TR-1000。
        self.service.publish_reuse_rules("eng", "R1", "规则v2", {
            "iron_core": {"allowed_product_models": ["TR-2000"],
                          "scrap_dispositions": ["material_recovery"]},
            "copper_winding": {"allowed_product_models": ["TR-2000"],
                               "scrap_dispositions": ["material_recovery"]},
            "gasket": {"allowed_product_models": [], "scrap_dispositions": ["waste"]},
        })
        self._device()
        self._inspect_pass("CORE1")
        self.service.assemble_product("eng", "SN2", "TR-1000")
        with self.assertRaises(InvalidState):
            self.service.bind_component_to_product("eng", "SN2", "CORE1", "R1")

    def test_inspection_requires_valid_calibration_at_measurement_time(self) -> None:
        self._device()
        with self.assertRaises(InvalidState):
            self._inspect_pass("CORE1", at="2027-06-01T00:00:00Z")
        with self.assertRaises(InvalidState):
            self._inspect_pass("CORE1", at="2025-12-31T00:00:00Z")

    def test_calibration_failure_locates_certificates_and_holds_undelivered(self) -> None:
        self._device("D1")
        self._inspect_pass("CORE1")
        self._inspect_pass("WIND1")
        cert1 = self._certified_product("SN-DELIVERED")
        self.service.deliver_product("qa", "SN-DELIVERED")

        self._device("D2", components=("CORE2",))
        self._inspect_pass("CORE2", at="2026-09-03T09:00:00Z")
        cert2 = self._certified_product("SN-HELD", components=("CORE2",))

        incident = self.service.report_calibration_failure("qa", "M1", "校准失效")
        self.assertEqual(set(incident["affected_certificates"]), {cert1, cert2})
        self.assertEqual(incident["held_serials"], ["SN-HELD"])
        self.assertEqual(incident["delivered_serials"], ["SN-DELIVERED"])
        # 只有未交付产品的认证进入暂停表；已交付认证被定位但不暂停。
        self.assertEqual(set(incident["held_certificates"]), {cert2})
        held_rows = {r["certificate_number"] for r in self.connection.execute(
            "SELECT certificate_number FROM certification_holds").fetchall()}
        self.assertEqual(held_rows, {cert2})
        # 已交付产品状态不变；未交付产品被暂停、无法交付。
        self.assertEqual(self.service.get_product("SN-DELIVERED")["state"], "delivered")
        with self.assertRaises(InvalidState):
            self.service.deliver_product("qa", "SN-HELD")
        # 暂停期间不能给新产品签发认证。
        with self.assertRaises(InvalidState):
            self.service.certify_product("qa", "SN-HELD", "R1", "certified", "重试")
        # 解除暂停后可交付。
        self.service.release_hold("qa", "SN-HELD", "重新校准合格")
        self.assertEqual(self.service.deliver_product("qa", "SN-HELD")["state"], "delivered")

    def test_product_can_be_re_held_after_release(self) -> None:
        self._device("D1")
        self._inspect_pass("CORE1")
        self._certified_product("SN-H", components=("CORE1",))
        first = self.service.report_calibration_failure("qa", "M1", "第一次失效")
        self.assertEqual(first["held_serials"], ["SN-H"])
        self.service.release_hold("qa", "SN-H", "复查通过")
        # 尚未交付时再次报校准失效，应能重新暂停。
        second = self.service.report_calibration_failure("qa", "M1", "再次失效")
        self.assertEqual(second["incident_id"], first["incident_id"] + 1)
        self.assertEqual(second["held_serials"], ["SN-H"])
        with self.assertRaises(InvalidState):
            self.service.deliver_product("qa", "SN-H")
        hold_rows = self.connection.execute(
            "SELECT state FROM product_holds WHERE serial_number='SN-H' ORDER BY hold_id"
        ).fetchall()
        self.assertEqual([row["state"] for row in hold_rows], ["released", "held"])

    def test_revoked_calibration_blocks_new_inspection(self) -> None:
        self._device()
        self.service.report_calibration_failure("qa", "M1", "失效")
        with self.assertRaises(InvalidState):
            self._inspect_pass("CORE1")

    def test_failed_inspection_blocks_reuse(self) -> None:
        self._device()
        self.service.record_inspection(
            "insp", "CORE1", "loss_test", "M1", "2026-09-02T09:00:00Z", "fail", data={"loss_w": 9000}
        )
        self.service.assemble_product("eng", "SN3", "TR-2000")
        with self.assertRaises(InvalidState):
            self.service.bind_component_to_product("eng", "SN3", "CORE1", "R1")

    def test_repair_requires_prior_inspection_and_pinned_spec(self) -> None:
        self._device()
        with self.assertRaises(InvalidState):
            self.service.repair_component("eng", "CORE1", "PROC")
        self._inspect_pass("CORE1")
        record = self.service.repair_component("eng", "CORE1", "PROC")
        self.assertEqual(record["process"], "PROC@1")
        # 显式指定旧版本，即使已发布新版本也按旧版本执行。
        self.service.publish_process_spec("eng", "PROC", "铁芯工艺v2", ["清理", "退火", "复检"])
        record = self.service.repair_component("eng", "CORE1", "PROC", process_version=1,
                                               evidence={"again": True})
        self.assertEqual(record["process"], "PROC@1")

    def test_component_cannot_be_double_disposed(self) -> None:
        self._device()
        self._inspect_pass("WIND1")
        self.service.scrap_component("disasm", "WIND1", "R1", "material_recovery")
        with self.assertRaises(InvalidState):
            self.service.scrap_component("disasm", "WIND1", "R1", "material_recovery")
        # 已报废部件不能装入产品。
        self.service.assemble_product("eng", "SN4", "TR-2000")
        with self.assertRaises(InvalidState):
            self.service.bind_component_to_product("eng", "SN4", "WIND1", "R1")

    def test_bound_component_cannot_be_scrapped_directly(self) -> None:
        self._device()
        self._inspect_pass("CORE1")
        self.service.assemble_product("eng", "SN5", "TR-2000")
        self.service.bind_component_to_product("eng", "SN5", "CORE1", "R1")
        with self.assertRaises(InvalidState):
            self.service.scrap_component("disasm", "CORE1", "R1", "material_recovery")

    def test_rejected_certification_releases_components(self) -> None:
        self._device()
        self._inspect_pass("CORE1")
        self._inspect_pass("WIND1")
        self.service.assemble_product("eng", "SN6", "TR-2000")
        self.service.bind_component_to_product("eng", "SN6", "CORE1", "R1")
        self.service.bind_component_to_product("eng", "SN6", "WIND1", "R1")
        self.service.certify_product("qa", "SN6", "R1", "rejected", "存疑，拒绝认证")
        self.assertEqual(self.service.get_product("SN6")["state"], "rejected")
        # 部件释放后可以走报废。
        result = self.service.scrap_component("disasm", "WIND1", "R1", "hazardous_disposal")
        self.assertEqual(result["scrap_disposition"], "hazardous_disposal")

    def test_duplicate_disassembly_blocked(self) -> None:
        self._device()
        with self.assertRaises(InvalidState):
            self.service.disassemble("disasm", "D1", [
                {"component_id": "CORE9", "component_type": "iron_core", "weight_kg": 1.0}
            ])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_device("disasm", "DX", "transformer", "x", 1.0)
        with self.assertRaises(Forbidden):
            self.service.publish_reuse_rules("qa", "R2", "x", {"a": {"scrap_dispositions": ["waste"]}})
        with self.assertRaises(Forbidden):
            self.service.certify_product("eng", "SN", "R1", "certified", "x")
        with self.assertRaises(Forbidden):
            self.service.device_lineage("intake", "D1")

    def test_unknown_user_and_bad_inputs(self) -> None:
        with self.assertRaises(NotFound):
            self.service.register_device("nobody", "D9", "transformer", "x", 1.0)
        with self.assertRaises(ValidationFailed):
            self.service.create_user("u", "n", "king")
        with self.assertRaises(ValidationFailed):
            self.service.publish_reuse_rules("eng", "R3", "空规则", {})
        with self.assertRaises(ValidationFailed):
            self.service.publish_reuse_rules("eng", "R4", "死胡同", {
                "x": {"allowed_product_models": [], "scrap_dispositions": []}
            })
        with self.assertRaises(ValidationFailed):
            self.service.record_calibration(
                "insp", "M1", "2027-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "C"
            )

    def test_revoke_certificate_keeps_history(self) -> None:
        self._device()
        self._inspect_pass("CORE1")
        self._inspect_pass("WIND1")
        certificate = self._certified_product("SN7")
        self.service.revoke_certificate("qa", certificate, "事后复查不合格")
        evidence = self.service.certificate_evidence("aud", certificate)
        self.assertEqual(evidence["certificate"]["state"], "revoked")
        self.assertIsNotNone(evidence["certificate"]["revoked_at"])
        with self.assertRaises(InvalidState):
            self.service.revoke_certificate("qa", certificate, "再次撤销")


if __name__ == "__main__":
    unittest.main()
