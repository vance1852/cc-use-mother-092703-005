"""无第三方依赖的再制造追踪 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import RemanufactureError, ValidationFailed
from .service import RemanufactureService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: RemanufactureService) -> None:
        self.service = service

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

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        service = self.service
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, service.create_user(payload["user_id"], payload["display_name"], payload["role"]))

            if method == "POST" and path == "/devices":
                return Response(201, service.register_device(actor, payload))
            if method == "GET" and len(parts) == 3 and parts[0] == "devices" and parts[2] == "genealogy":
                return Response(200, service.genealogy(parts[1]))

            if method == "POST" and path == "/disassemblies":
                return Response(201, service.disassemble(
                    actor, payload["disassembly_id"], payload["device_id"], payload["dismantled_at"],
                    payload["components"], payload.get("residue_weight_kg", "0"),
                    payload.get("residue_destination", ""), payload.get("note", "")))

            if method == "POST" and path == "/equipment":
                return Response(201, service.register_equipment(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "equipment" and parts[2] == "recalibrate":
                return Response(200, service.recalibrate(
                    actor, parts[1], payload["calibrated_at"], payload["valid_from"],
                    payload["valid_to"], payload["certificate_ref"]))

            if method == "POST" and path == "/inspections":
                return Response(201, service.record_inspection(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "inspections":
                return Response(200, service.inspection(parts[1]))

            if method == "POST" and path == "/process-specs":
                return Response(201, service.publish_process_spec(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "process-specs":
                return Response(200, service.process_spec(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "process-specs":
                return Response(200, service.process_spec(parts[1], int(parts[2])))
            if method == "POST" and len(parts) == 3 and parts[0] == "process-specs" and parts[2] == "retire":
                return Response(200, service.retire_process_spec(actor, parts[1]))

            if method == "POST" and path == "/reuse-policies":
                return Response(201, service.publish_reuse_policy(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "reuse-policies":
                return Response(200, service.reuse_policy(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "reuse-policies":
                return Response(200, service.reuse_policy(parts[1], int(parts[2])))
            if method == "POST" and len(parts) == 3 and parts[0] == "reuse-policies" and parts[2] == "retire":
                return Response(200, service.retire_reuse_policy(actor, parts[1]))

            if method == "POST" and path == "/repairs":
                return Response(201, service.repair_component(actor, payload))

            if method == "POST" and path == "/certifications":
                return Response(201, service.issue_certification(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "certifications":
                return Response(200, service.certification(parts[1]))

            if method == "POST" and path == "/products":
                return Response(201, service.build_product(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "assign":
                return Response(201, service.assign_to_product(
                    actor, parts[1], payload["component_ids"], payload.get("idempotency_key")))
            if method == "POST" and len(parts) == 3 and parts[0] == "products" and parts[2] == "deliver":
                return Response(200, service.deliver_product(
                    actor, parts[1], payload["delivered_at"], payload["customer"]))

            if method == "POST" and path == "/scrap":
                return Response(201, service.scrap_component(
                    actor, payload["disposition_id"], payload["component_id"], payload["destination"],
                    payload["policy_id"], payload.get("note", ""), payload.get("idempotency_key")))

            if method == "POST" and path == "/calibration-incidents":
                return Response(201, service.report_calibration_incident(
                    actor, payload["incident_id"], payload["equipment_id"], payload["invalid_from"],
                    payload["reason"], payload.get("detected_at")))
            if method == "GET" and len(parts) == 2 and parts[0] == "calibration-incidents":
                return Response(200, service.incident(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "calibration-incidents" and parts[2] == "resolve":
                return Response(200, service.resolve_calibration_incident(
                    actor, parts[1], payload["resolution"], bool(payload.get("release", False))))

            if method == "GET" and path == "/reports/material-destination":
                device_id = query.get("device_id", [None])[0]
                return Response(200, service.material_destination_report(actor, device_id))
            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "reuse-evidence":
                return Response(200, service.reuse_decision_evidence(actor, parts[1]))

            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RemanufactureError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReManTrack/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
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
    parser = argparse.ArgumentParser(description="启动绿色再制造与再认证追踪服务")
    parser.add_argument("--database", type=Path, default=Path("remanufacture.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(RemanufactureService(connection))))
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
