"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import RemanufacturingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: RemanufacturingService) -> None:
        self.service = service
        # SQLite 连接被多个 HTTP 工作线程共享，用一把可重入锁把请求整体串行化。
        self.gate = threading.RLock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)

            if method == "POST" and path == "/reuse-rules":
                result = self.service.publish_reuse_rules(
                    self._actor(normalized_headers), payload["rule_set_id"], payload["title"],
                    payload["rules"],
                )
                return Response(201, result)

            if method == "POST" and path == "/instruments":
                result = self.service.register_instrument(
                    self._actor(normalized_headers), payload["instrument_id"], payload["name"]
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "instruments" and parts[2] == "calibrations":
                result = self.service.record_calibration(
                    self._actor(normalized_headers), parts[1], payload["valid_from"],
                    payload["valid_until"], payload["certificate"],
                )
                return Response(201, result)

            if method == "POST" and path == "/devices":
                result = self.service.register_device(
                    self._actor(normalized_headers), payload["device_id"], payload["device_type"],
                    payload["source"], float(payload["recovered_weight_kg"]),
                    payload.get("manufacturer"), payload.get("model"), payload.get("received_at"),
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "devices" and parts[2] == "disassemble":
                result = self.service.disassemble(
                    self._actor(normalized_headers), parts[1], payload["components"]
                )
                return Response(200, result)

            if method == "GET" and len(parts) == 3 and parts[0] == "devices" and parts[2] == "lineage":
                result = self.service.device_lineage(self._actor(normalized_headers), parts[1])
                return Response(200, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "inspections":
                result = self.service.record_inspection(
                    self._actor(normalized_headers), parts[1], payload["method"],
                    payload["instrument_id"], payload["measured_at"], payload["result"],
                    payload.get("parameters"), payload.get("data"),
                )
                return Response(201, result)

            if method == "POST" and path == "/process-specs":
                result = self.service.publish_process_spec(
                    self._actor(normalized_headers), payload["process_code"], payload["title"],
                    payload["steps"], payload.get("requirements"),
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "repairs":
                result = self.service.repair_component(
                    self._actor(normalized_headers), parts[1], payload["process_code"],
                    payload.get("process_version"), payload.get("parameters"), payload.get("evidence"),
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "scrap":
                result = self.service.scrap_component(
                    self._actor(normalized_headers), parts[1], payload["rule_set_id"],
                    payload["scrap_disposition"], payload.get("destination"),
                )
                return Response(200, result)

            if method == "POST" and path == "/products":
                result = self.service.assemble_product(
                    self._actor(normalized_headers), payload["serial_number"], payload["product_model"]
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "components":
                result = self.service.bind_component_to_product(
                    self._actor(normalized_headers), parts[1], payload["component_id"],
                    payload["rule_set_id"],
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "certify":
                result = self.service.certify_product(
                    self._actor(normalized_headers), parts[1], payload["rule_set_id"],
                    payload["decision"], payload["reason"],
                )
                return Response(200, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "deliver":
                result = self.service.deliver_product(self._actor(normalized_headers), parts[1])
                return Response(200, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "release-hold":
                result = self.service.release_hold(
                    self._actor(normalized_headers), parts[1], payload.get("note", "")
                )
                return Response(200, result)

            if method == "POST" and path == "/calibration-incidents":
                result = self.service.report_calibration_failure(
                    self._actor(normalized_headers), payload["instrument_id"], payload["reason"],
                    payload.get("discovered_at"),
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "certificates" and parts[2] == "revoke":
                result = self.service.revoke_certificate(
                    self._actor(normalized_headers), parts[1], payload["reason"]
                )
                return Response(200, result)

            if method == "GET" and len(parts) == 3 and parts[0] == "certificates" and parts[2] == "evidence":
                result = self.service.certificate_evidence(self._actor(normalized_headers), parts[1])
                return Response(200, result)

            if method == "GET" and path == "/reports/material-destinations":
                result = self.service.material_destination_report(self._actor(normalized_headers))
                return Response(200, result)

            if method == "GET" and path == "/audit":
                result = self.service.audit_trail(
                    self._actor(normalized_headers),
                    query.get("entity_type", [None])[0],
                    query.get("entity_id", [None])[0],
                )
                return Response(200, {"events": result})

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Remanufacturing/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with application.gate:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动绿色再制造追踪与再认证 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("remanufacturing.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(RemanufacturingService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
