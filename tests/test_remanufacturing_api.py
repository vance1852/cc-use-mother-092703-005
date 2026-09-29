from __future__ import annotations

import json
import sqlite3
import unittest

from remanufacturing.api import JsonApplication
from remanufacturing.service import RemanufacturingService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(RemanufacturingService(self.connection))
        self._json = lambda body: json.dumps(body, ensure_ascii=False).encode()

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, body: dict, actor: str | None = "eng"):
        headers = {"X-Actor-Id": actor} if actor else {}
        return self.app.handle("POST", path, headers, self._json(body))

    def _seed_users(self) -> None:
        for uid, role in (
            ("intake", "intake"), ("disasm", "disassembly"), ("insp", "inspector"),
            ("eng", "process_engineer"), ("qa", "quality"), ("aud", "auditor"),
        ):
            self.app.handle("POST", "/users", body=self._json(
                {"user_id": uid, "display_name": uid, "role": role}))

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("POST", "/devices", body=self._json({"x": 1}))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_bad_json_is_422(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)

    def test_workflow_over_http_and_rule_violation(self) -> None:
        self._seed_users()
        response = self._post("/reuse-rules", {
            "rule_set_id": "R1", "title": "规则",
            "rules": {"iron_core": {"allowed_product_models": ["TR-2000"],
                                    "scrap_dispositions": ["material_recovery"]}},
        })
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["version"], 1)

        self.assertEqual(self._post("/instruments",
                                    {"instrument_id": "M1", "name": "损耗仪"},
                                    actor="insp").status, 201)
        response = self._post("/instruments/M1/calibrations", {
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
            "certificate": "C1",
        }, actor="insp")
        self.assertEqual(response.status, 201)

        response = self._post("/devices", {
            "device_id": "D1", "device_type": "transformer", "source": "退役批次",
            "recovered_weight_kg": 1000.0,
        }, actor="intake")
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["recovered_weight_kg"], "1000.000")

        response = self._post("/devices/D1/disassemble", {"components": [
            {"component_id": "CORE1", "component_type": "iron_core", "weight_kg": 400.0},
        ]}, actor="disasm")
        self.assertEqual(response.status, 200)

        response = self._post("/components/CORE1/inspections", {
            "method": "loss_test", "instrument_id": "M1",
            "measured_at": "2026-09-02T09:00:00Z", "result": "pass", "data": {"loss_w": 1200},
        }, actor="insp")
        self.assertEqual(response.status, 201)

        response = self._post("/products",
                              {"serial_number": "SN1", "product_model": "TR-1000"})
        self.assertEqual(response.status, 201)
        # 规则不允许 iron_core 进入 TR-1000。
        response = self._post("/products/SN1/components",
                              {"component_id": "CORE1", "rule_set_id": "R1"})
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["code"], "invalid_state")

        # 允许的型号可绑定、认证。
        self.assertEqual(self._post("/products",
                                    {"serial_number": "SN2", "product_model": "TR-2000"}).status, 201)
        response = self._post("/products/SN2/components",
                              {"component_id": "CORE1", "rule_set_id": "R1"})
        self.assertEqual(response.status, 201)
        response = self._post("/products/SN2/certify",
                              {"rule_set_id": "R1", "decision": "certified", "reason": "合格"},
                              actor="qa")
        self.assertEqual(response.status, 200)
        certificate = response.body["certificate_number"]

        response = self.app.handle(
            "GET", f"/certificates/{certificate}/evidence", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["certificate"]["rule"], "R1@1")

        response = self.app.handle("GET", "/reports/material-destinations",
                                   {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["reused_count"], 1)

    def test_forbidden_role(self) -> None:
        self._seed_users()
        response = self._post("/reuse-rules", {
            "rule_set_id": "R1", "title": "规则",
            "rules": {"iron_core": {"scrap_dispositions": ["waste"]}},
        }, actor="qa")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
