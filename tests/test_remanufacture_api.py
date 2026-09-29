from __future__ import annotations

import json
import sqlite3
import unittest

from remanufacture.api import JsonApplication
from remanufacture.service import RemanufactureService


class RemanufactureApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(RemanufactureService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "engineer"):
        return self.app.handle(
            "POST", path, headers={"X-Actor-Id": actor},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required(self) -> None:
        response = self.app.handle(
            "POST", "/reuse-policies",
            body=json.dumps({"policy_id": "P", "title": "t", "rules": {}}).encode())
        self.assertEqual(response.status, 422)

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/devices", headers={"X-Actor-Id": "intake"}, body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_end_to_end_via_http(self) -> None:
        for user_id, role in (("intake", "intake"), ("dismantler", "dismantler"),
                              ("inspector", "inspector"), ("engineer", "engineer"),
                              ("certifier", "certifier"), ("logistics", "logistics"),
                              ("auditor", "auditor")):
            self._post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
        device = self._post("/devices", {
            "device_id": "D1", "device_type": "oil-transformer", "model": "S13",
            "serial_no": "SN-1", "source": "退运", "recovered_weight_kg": "100",
            "recovered_at": "2026-09-28T00:00:00Z"}, actor="intake")
        self.assertEqual(device.status, 201)
        dis = self._post("/disassemblies", {
            "disassembly_id": "DIS1", "device_id": "D1", "dismantled_at": "2026-09-29T00:00:00Z",
            "components": [
                {"component_id": "core1", "component_type": "iron_core", "name": "铁芯", "weight_kg": "90"},
                {"component_id": "tank1", "component_type": "steel_tank", "name": "油箱", "weight_kg": "10"},
            ]}, actor="dismantler")
        self.assertEqual(dis.status, 201)
        self.assertEqual(self.app.handle(
            "GET", "/devices/D1/genealogy", headers={"X-Actor-Id": "auditor"}).status, 200)
        eq = self._post("/equipment", {
            "equipment_id": "EQ1", "name": "变比仪", "serial_no": "X1",
            "calibration_valid_from": "2026-01-01T00:00:00Z",
            "calibration_valid_to": "2026-12-31T23:59:59Z"}, actor="inspector")
        self.assertEqual(eq.status, 201)
        insp = self._post("/inspections", {
            "inspection_id": "I1", "component_id": "core1", "method_code": "M1",
            "method_name": "变比", "equipment_id": "EQ1",
            "inspected_at": "2026-10-01T00:00:00Z", "result": "pass"}, actor="inspector")
        self.assertEqual(insp.status, 201)
        policy = self._post("/reuse-policies", {
            "policy_id": "P1", "title": "规则",
            "rules": {"iron_core": {"allow_new_products": ["distribution-transformer"], "allow_scrap": False},
                      "steel_tank": {"allow_new_products": [], "allow_scrap": True}}})
        self.assertEqual(policy.status, 201)
        cert = self._post("/certifications", {
            "certification_id": "C1", "batch_no": "B1",
            "product_type": "distribution-transformer",
            "component_ids": ["core1"], "policy_id": "P1"}, actor="certifier")
        self.assertEqual(cert.status, 201)
        product = self._post("/products", {
            "product_id": "RX1", "certification_id": "C1", "built_at": "2026-10-06T00:00:00Z"})
        self.assertEqual(product.status, 201)
        assigned = self._post("/products/RX1/assign", {"component_ids": ["core1"]})
        self.assertEqual(assigned.status, 201)
        scrap = self._post("/scrap", {
            "disposition_id": "DP1", "component_id": "tank1",
            "destination": "废钢堆场", "policy_id": "P1"}, actor="logistics")
        self.assertEqual(scrap.status, 201)
        report = self.app.handle(
            "GET", "/reports/material-destination?device_id=D1", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(report.body["weight_summary_kg"]["new_product"], "90")
        evidence = self.app.handle(
            "GET", "/components/core1/reuse-evidence", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(evidence.status, 200)
        self.assertEqual(evidence.body["certification"]["certification_id"], "C1")
        audit = self.app.handle("GET", "/audit/chain", headers={"X-Actor-Id": "auditor"})
        self.assertTrue(audit.body["valid"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
