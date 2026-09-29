"""再制造追踪与再认证的领域用例。

业务链条：
回收设备登记 -> 拆解出部件（谱系）-> 部件检测（引用设备校准有效期）
-> 修复（引用不可变工艺规范版本）-> 规则允许的去向（新产品复用或报废）
-> 新产品再认证（固化规则/检测/工艺证据版本，签发后不可被规范更新改写）
-> 校准失效时定位受影响认证批次并暂停尚未交付的产品。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat, parse_iso
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, set[str]] = {
    "intake": {"device.register"},
    "disassembly": {"device.disassemble", "component.scrap"},
    "inspector": {"instrument.register", "calibration.record", "inspection.record"},
    "process_engineer": {"rule.publish", "process.publish", "repair.record", "product.assemble"},
    "quality": {"certify", "incident.report", "hold.release", "certificate.revoke"},
    "auditor": {"trace.read", "report.read", "audit.read"},
}

SCRAP_DISPOSITIONS = ("material_recovery", "hazardous_disposal", "waste")


class RemanufacturingService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础辅助 -------------------------------------------------------

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 不能为空")
        return value.strip()

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ----- 复用规则（版本化）---------------------------------------------

    def publish_reuse_rules(self, actor_id: str, rule_set_id: str, title: str, rules: Mapping[str, Any]) -> dict[str, Any]:
        """发布一版不可变复用规则。

        rules: {component_type: {"allowed_product_models": [...], "scrap_dispositions": [...]}}
        每个部件类型必须至少允许一种新产品型号或一种报废处置。
        """

        self._require(actor_id, "rule.publish")
        rule_set_id = self._require_text(rule_set_id, "rule_set_id")
        title = self._require_text(title, "title")
        items = self._normalize_rule_items(rules)
        document = {"rule_set_id": rule_set_id, "title": title, "rules": items}
        digest = content_digest([document])
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT version FROM reuse_rule_versions WHERE rule_set_id=? ORDER BY version DESC LIMIT 1",
                (rule_set_id,),
            ).fetchone()
            duplicate = self.connection.execute(
                "SELECT 1 FROM reuse_rule_versions WHERE content_sha256=?", (digest,)
            ).fetchone()
            if duplicate is not None:
                raise Conflict("该规则内容已发布，不能重复建立版本")
            version = 1 if latest is None else latest["version"] + 1
            self.connection.execute(
                "UPDATE reuse_rule_versions SET state='superseded' WHERE rule_set_id=? AND state='active'",
                (rule_set_id,),
            )
            self.connection.execute(
                "INSERT INTO reuse_rule_versions"
                "(rule_set_id,version,title,canonical_json,content_sha256,state,published_by,published_at) "
                "VALUES(?,?,?,?,?, 'active', ?,?)",
                (rule_set_id, version, title, canonical_json(document), digest, actor_id, self._now()),
            )
            for component_type, item in items.items():
                self.connection.execute(
                    "INSERT INTO reuse_rule_items"
                    "(rule_set_id,version,component_type,allowed_product_models,scrap_dispositions) "
                    "VALUES(?,?,?,?,?)",
                    (
                        rule_set_id, version, component_type,
                        canonical_json(item["allowed_product_models"]),
                        canonical_json(item["scrap_dispositions"]),
                    ),
                )
            self._audit("reuse_rule_set", rule_set_id, "rules.published", actor_id,
                        {"version": version, "sha256": digest})
        return {"rule_set_id": rule_set_id, "version": version, "sha256": digest}

    def _normalize_rule_items(self, rules: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
        if not isinstance(rules, dict) or not rules:
            raise ValidationFailed("规则至少要包含一种部件类型")
        normalized: dict[str, dict[str, list[str]]] = {}
        for raw_type, raw_item in rules.items():
            component_type = self._require_text(raw_type, "component_type")
            if not isinstance(raw_item, dict):
                raise ValidationFailed(f"{component_type} 的规则必须是对象")
            models = self._normalize_string_list(raw_item.get("allowed_product_models", []),
                                                f"{component_type}.allowed_product_models")
            scraps = self._normalize_string_list(raw_item.get("scrap_dispositions", []),
                                                 f"{component_type}.scrap_dispositions")
            unknown = sorted(set(scraps) - set(SCRAP_DISPOSITIONS))
            if unknown:
                raise ValidationFailed(f"未知报废处置: {', '.join(unknown)}")
            if not models and not scraps:
                raise ValidationFailed(f"{component_type} 必须允许至少一种新产品型号或报废处置")
            normalized[component_type] = {"allowed_product_models": models, "scrap_dispositions": scraps}
        return normalized

    @staticmethod
    def _normalize_string_list(value: Any, field: str) -> list[str]:
        if not isinstance(value, list):
            raise ValidationFailed(f"{field} 必须是数组")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValidationFailed(f"{field} 含空值")
            result.append(item.strip())
        if len(result) != len(set(result)):
            raise ValidationFailed(f"{field} 含重复值")
        return result

    def _active_rule_version(self, rule_set_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reuse_rule_versions WHERE rule_set_id=? AND state='active' "
            "ORDER BY version DESC LIMIT 1",
            (rule_set_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"规则集没有生效版本: {rule_set_id}")
        return row

    def _rule_item(self, rule_set_id: str, version: int, component_type: str) -> dict[str, list[str]]:
        row = self.connection.execute(
            "SELECT allowed_product_models,scrap_dispositions FROM reuse_rule_items "
            "WHERE rule_set_id=? AND version=? AND component_type=?",
            (rule_set_id, version, component_type),
        ).fetchone()
        if row is None:
            raise InvalidState(f"规则 {rule_set_id}@{version} 未覆盖部件类型 {component_type}")
        return {
            "allowed_product_models": json.loads(row["allowed_product_models"]),
            "scrap_dispositions": json.loads(row["scrap_dispositions"]),
        }

    # ----- 设备与校准 ------------------------------------------------------

    def register_instrument(self, actor_id: str, instrument_id: str, name: str) -> dict[str, Any]:
        self._require(actor_id, "instrument.register")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO instruments(instrument_id,name,created_at) VALUES(?,?,?)",
                    (instrument_id, name, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"检测设备已存在: {instrument_id}") from exc
        return {"instrument_id": instrument_id, "name": name}

    def record_calibration(
        self, actor_id: str, instrument_id: str, valid_from: str, valid_until: str, certificate: str
    ) -> dict[str, Any]:
        self._require(actor_id, "calibration.record")
        start = parse_iso(valid_from)
        end = parse_iso(valid_until)
        if not start < end:
            raise ValidationFailed("校准生效时间必须早于失效时间")
        certificate = self._require_text(certificate, "certificate")
        if self.connection.execute("SELECT 1 FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone() is None:
            raise NotFound(f"检测设备不存在: {instrument_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO calibrations"
                "(instrument_id,valid_from,valid_until,state,certificate,recorded_by,created_at) "
                "VALUES(?,?,?, 'valid', ?,?,?)",
                (instrument_id, isoformat(start), isoformat(end), certificate, actor_id, self._now()),
            )
            calibration_id = cursor.lastrowid
            self._audit("instrument", instrument_id, "calibration.recorded", actor_id,
                        {"calibration_id": calibration_id, "valid_until": isoformat(end)})
        return {"calibration_id": calibration_id, "instrument_id": instrument_id,
                "valid_from": isoformat(start), "valid_until": isoformat(end)}

    def _calibration_at(self, instrument_id: str, measured_at: str) -> sqlite3.Row:
        """返回测量时刻处于有效期内、且当时未被撤销的校准。"""

        row = self.connection.execute(
            "SELECT * FROM calibrations WHERE instrument_id=? AND state='valid' "
            "AND valid_from<=? AND valid_until>? ORDER BY valid_from DESC LIMIT 1",
            (instrument_id, measured_at, measured_at),
        ).fetchone()
        if row is None:
            raise InvalidState(f"检测设备 {instrument_id} 在测量时刻没有有效校准")
        return row

    # ----- 回收设备与拆解 --------------------------------------------------

    def register_device(
        self, actor_id: str, device_id: str, device_type: str, source: str,
        recovered_weight_kg: float, manufacturer: str | None = None, model: str | None = None,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "device.register")
        weight = f"{float(recovered_weight_kg):.3f}"
        if float(weight) < 0:
            raise ValidationFailed("回收重量不能为负")
        received = isoformat(parse_iso(received_at)) if received_at else self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recovered_devices"
                    "(device_id,device_type,manufacturer,model,source,recovered_weight_kg,"
                    "received_at,state,registered_by,created_at) VALUES(?,?,?,?,?,?,?, 'received', ?,?)",
                    (device_id, device_type, manufacturer, model, source, weight,
                     received, actor_id, self._now()),
                )
                self._audit("device", device_id, "device.registered", actor_id,
                            {"device_type": device_type, "recovered_weight_kg": weight})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"回收设备已存在: {device_id}") from exc
        return self.get_device(device_id)

    def get_device(self, device_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM recovered_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            raise NotFound(f"回收设备不存在: {device_id}")
        return dict(row)

    def disassemble(
        self, actor_id: str, device_id: str, components: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """把回收设备拆解为部件，建立 device -> component 谱系。"""

        self._require(actor_id, "device.disassemble")
        if not components:
            raise ValidationFailed("拆解部件清单不能为空")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in components:
            component_id = self._require_text(item.get("component_id"), "component_id")
            if component_id in seen:
                raise ValidationFailed(f"部件编号重复: {component_id}")
            seen.add(component_id)
            component_type = self._require_text(item.get("component_type"), "component_type")
            weight = item.get("weight_kg")
            if weight is not None:
                weight = f"{float(weight):.3f}"
                if float(weight) < 0:
                    raise ValidationFailed("部件重量不能为负")
            normalized.append({
                "component_id": component_id,
                "component_type": component_type,
                "material": item.get("material"),
                "weight_kg": weight,
            })
        with transaction(self.connection, immediate=True):
            device = self.connection.execute(
                "SELECT state FROM recovered_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if device is None:
                raise NotFound(f"回收设备不存在: {device_id}")
            if device["state"] != "received":
                raise InvalidState("设备已拆解或已报废，不能重复拆解")
            now = self._now()
            for item in normalized:
                try:
                    self.connection.execute(
                        "INSERT INTO components"
                        "(component_id,device_id,component_type,material,weight_kg,disassembled_at,"
                        "state,disassembled_by,created_at) VALUES(?,?,?,?,?,?,'recovered',?,?)",
                        (item["component_id"], device_id, item["component_type"], item["material"],
                         item["weight_kg"], now, actor_id, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"部件编号冲突: {item['component_id']}") from exc
            self.connection.execute(
                "UPDATE recovered_devices SET state='disassembled' WHERE device_id=? AND state='received'",
                (device_id,),
            )
            self._audit("device", device_id, "device.disassembled", actor_id,
                        {"components": [item["component_id"] for item in normalized]})
        return {"device_id": device_id, "component_count": len(normalized),
                "components": normalized}

    def get_component(self, component_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM components WHERE component_id=?", (component_id,)).fetchone()
        if row is None:
            raise NotFound(f"部件不存在: {component_id}")
        return dict(row)

    # ----- 检测 ------------------------------------------------------------

    def record_inspection(
        self, actor_id: str, component_id: str, method: str, instrument_id: str,
        measured_at: str, result: str, parameters: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "inspection.record")
        if result not in {"pass", "fail"}:
            raise ValidationFailed("检测结果只能是 pass 或 fail")
        method = self._require_text(method, "method")
        when = isoformat(parse_iso(measured_at))
        component = self.connection.execute(
            "SELECT state FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if component is None:
            raise NotFound(f"部件不存在: {component_id}")
        calibration = self._calibration_at(instrument_id, when)
        parameters = dict(parameters or {})
        data = dict(data or {})
        evidence = {
            "component_id": component_id,
            "method": method,
            "parameters": parameters,
            "instrument_id": instrument_id,
            "calibration_id": calibration["calibration_id"],
            "calibration_valid_until": calibration["valid_until"],
            "measured_at": when,
            "result": result,
            "data": data,
        }
        digest = content_digest([evidence])
        inspection_id = f"INS-{component_id}-{digest[:12]}"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO inspections"
                    "(inspection_id,component_id,method,parameters_json,instrument_id,calibration_id,"
                    "measured_at,result,data_json,evidence_sha256,inspected_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (inspection_id, component_id, method, canonical_json(parameters), instrument_id,
                     calibration["calibration_id"], when, result, canonical_json(data), digest,
                     actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("相同检测证据已存在") from exc
            if component["state"] == "recovered":
                self.connection.execute(
                    "UPDATE components SET state='inspected' WHERE component_id=? AND state='recovered'",
                    (component_id,),
                )
            self._audit("component", component_id, "inspection.recorded", actor_id,
                        {"inspection_id": inspection_id, "result": result,
                         "calibration_id": calibration["calibration_id"], "sha256": digest})
        return {"inspection_id": inspection_id, "component_id": component_id, "result": result,
                "calibration_id": calibration["calibration_id"], "evidence_sha256": digest}

    # ----- 修复工艺（版本化）----------------------------------------------

    def publish_process_spec(
        self, actor_id: str, process_code: str, title: str, steps: Sequence[str],
        requirements: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "process.publish")
        process_code = self._require_text(process_code, "process_code")
        title = self._require_text(title, "title")
        step_list = self._normalize_string_list(list(steps or []), "steps")
        if not step_list:
            raise ValidationFailed("工艺至少包含一个步骤")
        document = {
            "process_code": process_code,
            "title": title,
            "steps": step_list,
            "requirements": dict(requirements or {}),
        }
        digest = content_digest([document])
        with transaction(self.connection, immediate=True):
            if self.connection.execute(
                "SELECT 1 FROM process_spec_versions WHERE content_sha256=?", (digest,)
            ).fetchone() is not None:
                raise Conflict("该工艺内容已发布，不能重复建立版本")
            latest = self.connection.execute(
                "SELECT version FROM process_spec_versions WHERE process_code=? ORDER BY version DESC LIMIT 1",
                (process_code,),
            ).fetchone()
            version = 1 if latest is None else latest["version"] + 1
            self.connection.execute(
                "UPDATE process_spec_versions SET state='superseded' WHERE process_code=? AND state='active'",
                (process_code,),
            )
            self.connection.execute(
                "INSERT INTO process_spec_versions"
                "(process_code,version,title,canonical_json,content_sha256,state,published_by,published_at) "
                "VALUES(?,?,?,?,?, 'active', ?,?)",
                (process_code, version, title, canonical_json(document), digest, actor_id, self._now()),
            )
            self._audit("process_spec", process_code, "process.published", actor_id,
                        {"version": version, "sha256": digest})
        return {"process_code": process_code, "version": version, "sha256": digest}

    def repair_component(
        self, actor_id: str, component_id: str, process_code: str,
        process_version: int | None = None, parameters: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "repair.record")
        component = self.connection.execute(
            "SELECT state FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if component is None:
            raise NotFound(f"部件不存在: {component_id}")
        if component["state"] not in {"inspected", "repaired"}:
            raise InvalidState("部件必须先通过检测才能修复")
        if process_version is None:
            spec = self.connection.execute(
                "SELECT * FROM process_spec_versions WHERE process_code=? AND state='active' "
                "ORDER BY version DESC LIMIT 1",
                (process_code,),
            ).fetchone()
            if spec is None:
                raise NotFound(f"工艺没有生效版本: {process_code}")
        else:
            spec = self.connection.execute(
                "SELECT * FROM process_spec_versions WHERE process_code=? AND version=?",
                (process_code, process_version),
            ).fetchone()
            if spec is None:
                raise NotFound(f"工艺版本不存在: {process_code}@{process_version}")
        parameters = dict(parameters or {})
        evidence = dict(evidence or {})
        record = {
            "component_id": component_id,
            "process_code": process_code,
            "process_version": spec["version"],
            "parameters": parameters,
            "evidence": evidence,
        }
        digest = content_digest([record])
        repair_id = f"REP-{component_id}-{digest[:12]}"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO repairs"
                    "(repair_id,component_id,process_code,process_version,parameters_json,"
                    "evidence_sha256,repaired_at,repaired_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (repair_id, component_id, process_code, spec["version"], canonical_json(parameters),
                     digest, self._now(), actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("相同修复记录已存在") from exc
            self.connection.execute(
                "UPDATE components SET state='repaired' WHERE component_id=?", (component_id,)
            )
            self._audit("component", component_id, "repair.recorded", actor_id,
                        {"repair_id": repair_id, "process": f"{process_code}@{spec['version']}",
                         "sha256": digest})
        return {"repair_id": repair_id, "component_id": component_id,
                "process": f"{process_code}@{spec['version']}", "evidence_sha256": digest}

    # ----- 报废 ------------------------------------------------------------

    def scrap_component(
        self, actor_id: str, component_id: str, rule_set_id: str, scrap_disposition: str,
        destination: str | None = None,
    ) -> dict[str, Any]:
        """部件按规则允许的报废处置离开再制造链条（不进入新产品）。"""

        self._require(actor_id, "component.scrap")
        component = self.connection.execute(
            "SELECT * FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if component is None:
            raise NotFound(f"部件不存在: {component_id}")
        if component["state"] in {"reused", "scrapped"}:
            raise InvalidState("部件已有最终去向")
        if self.connection.execute(
            "SELECT 1 FROM product_components WHERE component_id=?", (component_id,)
        ).fetchone() is not None:
            raise InvalidState("部件已装入新产品，不能直接报废；需先解除装配或拒绝认证")
        rule = self._active_rule_version(rule_set_id)
        item = self._rule_item(rule_set_id, rule["version"], component["component_type"])
        if scrap_disposition not in item["scrap_dispositions"]:
            raise InvalidState(
                f"规则 {rule_set_id}@{rule['version']} 不允许 {component['component_type']} "
                f"走 {scrap_disposition}，允许: {', '.join(item['scrap_dispositions']) or '无'}"
            )
        evidence = {
            "component_id": component_id, "kind": "scrap",
            "rule_set_id": rule_set_id, "rule_version": rule["version"],
            "scrap_disposition": scrap_disposition, "destination": destination,
        }
        digest = content_digest([evidence])
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO dispositions"
                    "(component_id,kind,rule_set_id,rule_version,serial_number,scrap_disposition,"
                    "destination,evidence_sha256,disposed_at,disposed_by) "
                    "VALUES(?, 'scrap', ?,?,NULL,?,?,?,?,?)",
                    (component_id, rule_set_id, rule["version"], scrap_disposition,
                     destination, digest, self._now(), actor_id),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("部件已有去向") from exc
            self.connection.execute(
                "UPDATE components SET state='scrapped' WHERE component_id=?", (component_id,)
            )
            self._audit("component", component_id, "component.scrapped", actor_id,
                        {"rule": f"{rule_set_id}@{rule['version']}",
                         "scrap_disposition": scrap_disposition, "sha256": digest})
        return {"component_id": component_id, "kind": "scrap",
                "scrap_disposition": scrap_disposition, "destination": destination,
                "rule": f"{rule_set_id}@{rule['version']}", "evidence_sha256": digest}

    # ----- 新产品装配与再认证 ----------------------------------------------

    def assemble_product(
        self, actor_id: str, serial_number: str, product_model: str
    ) -> dict[str, Any]:
        self._require(actor_id, "product.assemble")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO products"
                    "(serial_number,product_model,assembled_at,state,assembled_by,created_at) "
                    "VALUES(?,?,?, 'assembled', ?,?)",
                    (serial_number, product_model, self._now(), actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"产品序列号已存在: {serial_number}") from exc
            self._audit("product", serial_number, "product.assembled", actor_id,
                        {"product_model": product_model})
        return self.get_product(serial_number)

    def get_product(self, serial_number: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM products WHERE serial_number=?", (serial_number,)).fetchone()
        if row is None:
            raise NotFound(f"产品不存在: {serial_number}")
        return dict(row)

    def _component_evidence_basis(self, component: sqlite3.Row) -> dict[str, Any]:
        inspections = self.connection.execute(
            "SELECT inspection_id,method,instrument_id,calibration_id,measured_at,result,evidence_sha256 "
            "FROM inspections WHERE component_id=? ORDER BY measured_at,inspection_id",
            (component["component_id"],),
        ).fetchall()
        if not inspections:
            raise InvalidState(f"部件 {component['component_id']} 缺少检测记录，不能复用")
        if any(row["result"] != "pass" for row in inspections):
            raise InvalidState(f"部件 {component['component_id']} 存在不合格检测，不能复用")
        repairs = self.connection.execute(
            "SELECT repair_id,process_code,process_version,evidence_sha256 "
            "FROM repairs WHERE component_id=? ORDER BY repaired_at,repair_id",
            (component["component_id"],),
        ).fetchall()
        return {
            "component_id": component["component_id"],
            "component_type": component["component_type"],
            "inspections": [dict(row) for row in inspections],
            "repairs": [dict(row) for row in repairs],
        }

    def certify_product(
        self, actor_id: str, serial_number: str, rule_set_id: str, decision: str, reason: str
    ) -> dict[str, Any]:
        """对新产品做再认证。

        - 每个复用部件必须命中当前规则允许的产品型号；
        - 签发时固化规则版本、检测方法与校准、修复工艺版本的证据摘要；
        - 已签发认证不会因为之后规则/工艺更新而改变（不追溯改写）。
        """

        self._require(actor_id, "certify")
        if decision not in {"certified", "rejected"}:
            raise ValidationFailed("决定只能是 certified 或 rejected")
        reason = self._require_text(reason, "reason")
        product = self.connection.execute("SELECT * FROM products WHERE serial_number=?", (serial_number,)).fetchone()
        if product is None:
            raise NotFound(f"产品不存在: {serial_number}")
        if product["state"] != "assembled":
            raise InvalidState("只有装配完成、尚未认证的产品可以认证")
        rows = self.connection.execute(
            "SELECT c.* FROM product_components pc "
            "JOIN components c ON c.component_id=pc.component_id WHERE pc.serial_number=?",
            (serial_number,),
        ).fetchall()
        if not rows:
            raise InvalidState("产品尚未绑定任何部件")
        rule = self._active_rule_version(rule_set_id)
        basis_components: list[dict[str, Any]] = []
        rule_refs: list[dict[str, Any]] = []
        for component in rows:
            item = self._rule_item(rule_set_id, rule["version"], component["component_type"])
            if product["product_model"] not in item["allowed_product_models"]:
                raise InvalidState(
                    f"部件 {component['component_id']}（{component['component_type']}）按规则 "
                    f"{rule_set_id}@{rule['version']} 不能进入 {product['product_model']}，"
                    f"允许型号: {', '.join(item['allowed_product_models']) or '无'}"
                )
            basis_components.append(self._component_evidence_basis(component))
            rule_refs.append({
                "component_id": component["component_id"],
                "component_type": component["component_type"],
                "allowed_product_models": item["allowed_product_models"],
            })
        basis = {
            "serial_number": serial_number,
            "product_model": product["product_model"],
            "rule_set_id": rule_set_id,
            "rule_version": rule["version"],
            "rule_sha256": rule["content_sha256"],
            "components": basis_components,
        }
        digest = content_digest([basis])
        certificate_number = f"CERT-{serial_number}-{digest[:10]}"
        with transaction(self.connection, immediate=True):
            held = self.connection.execute(
                "SELECT 1 FROM product_holds WHERE serial_number=? AND state='held'", (serial_number,)
            ).fetchone()
            if held is not None:
                raise InvalidState("产品处于暂停状态，不能签发认证")
            self.connection.execute(
                "UPDATE product_components SET rule_set_id=?,rule_version=?,decided_at=?,decided_by=? "
                "WHERE serial_number=?",
                (rule_set_id, rule["version"], self._now(), actor_id, serial_number),
            )
            self.connection.execute(
                "INSERT INTO certifications"
                "(certificate_number,serial_number,rule_set_id,rule_version,evidence_sha256,decision,"
                "basis_json,state,issued_by,issued_at) VALUES(?,?,?,?,?,?,?, 'issued', ?,?)",
                (certificate_number, serial_number, rule_set_id, rule["version"], digest, decision,
                 canonical_json(basis), actor_id, self._now()),
            )
            if decision == "certified":
                self.connection.execute(
                    "UPDATE products SET state='certified' WHERE serial_number=?", (serial_number,)
                )
                for component in rows:
                    self.connection.execute(
                        "INSERT INTO dispositions"
                        "(component_id,kind,rule_set_id,rule_version,serial_number,scrap_disposition,"
                        "destination,evidence_sha256,disposed_at,disposed_by) "
                        "VALUES(?, 'reuse', ?,?,?,NULL,NULL,?, ?,?)",
                        (component["component_id"], rule_set_id, rule["version"], serial_number,
                         digest, self._now(), actor_id),
                    )
                    self.connection.execute(
                        "UPDATE components SET state='reused' WHERE component_id=?",
                        (component["component_id"],)
                    )
            else:
                # 拒绝认证：释放部件绑定，部件回到可处置状态（转报废或改装他品）。
                self.connection.execute(
                    "UPDATE products SET state='rejected' WHERE serial_number=?", (serial_number,)
                )
                self.connection.execute(
                    "DELETE FROM product_components WHERE serial_number=?", (serial_number,)
                )
            self._audit("product", serial_number, "certification.issued", actor_id,
                        {"certificate_number": certificate_number, "decision": decision,
                         "rule": f"{rule_set_id}@{rule['version']}", "sha256": digest})
        return {"certificate_number": certificate_number, "serial_number": serial_number,
                "decision": decision, "rule": f"{rule_set_id}@{rule['version']}",
                "evidence_sha256": digest}

    def bind_component_to_product(
        self, actor_id: str, serial_number: str, component_id: str, rule_set_id: str
    ) -> dict[str, Any]:
        """把部件装入新产品；规则只允许的型号才能接收该部件类型。"""

        self._require(actor_id, "product.assemble")
        product = self.connection.execute("SELECT * FROM products WHERE serial_number=?", (serial_number,)).fetchone()
        if product is None:
            raise NotFound(f"产品不存在: {serial_number}")
        if product["state"] != "assembled":
            raise InvalidState(f"产品状态为 {product['state']}，只有装配中的产品可以装入部件")
        component = self.connection.execute(
            "SELECT * FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if component is None:
            raise NotFound(f"部件不存在: {component_id}")
        if component["state"] not in {"inspected", "repaired"}:
            raise InvalidState("部件必须已检测合格才能装入新产品")
        failed = self.connection.execute(
            "SELECT 1 FROM inspections WHERE component_id=? AND result='fail' LIMIT 1",
            (component_id,),
        ).fetchone()
        if failed is not None:
            raise InvalidState("部件存在不合格检测记录，不能装入新产品；只能走规则允许的报废流程")
        rule = self._active_rule_version(rule_set_id)
        item = self._rule_item(rule_set_id, rule["version"], component["component_type"])
        if product["product_model"] not in item["allowed_product_models"]:
            raise InvalidState(
                f"规则 {rule_set_id}@{rule['version']} 不允许 {component['component_type']} "
                f"进入 {product['product_model']}，允许: {', '.join(item['allowed_product_models']) or '无'}"
            )
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO product_components"
                    "(serial_number,component_id,rule_set_id,rule_version,decided_at,decided_by) "
                    "VALUES(?,?,?,?,?,?)",
                    (serial_number, component_id, rule_set_id, rule["version"], self._now(), actor_id),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("部件已装入其他产品或已重复装入") from exc
            self._audit("product", serial_number, "component.bound", actor_id,
                        {"component_id": component_id,
                         "rule": f"{rule_set_id}@{rule['version']}"})
        return {"serial_number": serial_number, "component_id": component_id,
                "rule": f"{rule_set_id}@{rule['version']}"}

    def deliver_product(self, actor_id: str, serial_number: str) -> dict[str, Any]:
        self._require(actor_id, "certify")
        with transaction(self.connection, immediate=True):
            product = self.connection.execute(
                "SELECT state FROM products WHERE serial_number=?", (serial_number,)
            ).fetchone()
            if product is None:
                raise NotFound(f"产品不存在: {serial_number}")
            if self.connection.execute(
                "SELECT 1 FROM product_holds WHERE serial_number=? AND state='held'", (serial_number,)
            ).fetchone() is not None:
                raise InvalidState("产品处于暂停状态，不能交付")
            if product["state"] != "certified":
                raise InvalidState("只有已认证产品可以交付")
            self.connection.execute(
                "UPDATE products SET state='delivered' WHERE serial_number=? AND state='certified'",
                (serial_number,),
            )
            self._audit("product", serial_number, "product.delivered", actor_id, {})
        return {"serial_number": serial_number, "state": "delivered"}

    # ----- 校准失效：定位批次 + 暂停 ---------------------------------------

    def report_calibration_failure(
        self, actor_id: str, instrument_id: str, reason: str, discovered_at: str | None = None
    ) -> dict[str, Any]:
        """登记检测设备校准失效。

        定位测量时刻引用了该设备校准的全部认证（受影响认证批次），
        并暂停其中尚未交付的产品；已交付产品保持可追溯但不做状态改动。
        """

        self._require(actor_id, "incident.report")
        reason = self._require_text(reason, "reason")
        discovered = isoformat(parse_iso(discovered_at)) if discovered_at else self._now()
        calibrations = self.connection.execute(
            "SELECT calibration_id FROM calibrations WHERE instrument_id=?", (instrument_id,)
        ).fetchall()
        if not calibrations:
            raise NotFound(f"检测设备没有校准记录: {instrument_id}")
        affected_cert_rows = self.connection.execute(
            """
            SELECT DISTINCT cert.certificate_number, cert.serial_number, p.state AS product_state
            FROM inspections i
            JOIN components comp ON comp.component_id = i.component_id
            JOIN product_components pc ON pc.component_id = comp.component_id
            JOIN certifications cert ON cert.serial_number = pc.serial_number
            JOIN products p ON p.serial_number = cert.serial_number
            WHERE i.instrument_id = ?
            """,
            (instrument_id,),
        ).fetchall()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO calibration_incidents"
                "(calibration_id,reason,discovered_at,reported_by,created_at) "
                "VALUES((SELECT calibration_id FROM calibrations WHERE instrument_id=? "
                "ORDER BY valid_from DESC LIMIT 1),?,?,?,?)",
                (instrument_id, reason, discovered, actor_id, self._now()),
            )
            incident_id = cursor.lastrowid
            self.connection.execute(
                "UPDATE calibrations SET state='revoked' WHERE instrument_id=? AND state='valid'",
                (instrument_id,),
            )
            held_serials: list[str] = []
            delivered_serials: list[str] = []
            held_certificates: set[str] = set()
            affected_certificates: set[str] = set()
            for row in affected_cert_rows:
                # 所有引用了失效校准的认证都被定位（含已交付，供追溯/召回）。
                affected_certificates.add(row["certificate_number"])
                if row["product_state"] != "delivered":
                    # 仅对尚未交付的产品建立暂停，避免出现无法解除的"已交付暂停"。
                    if row["certificate_number"] not in held_certificates:
                        self.connection.execute(
                            "INSERT INTO certification_holds"
                            "(incident_id,certificate_number,state,created_at,note) "
                            "VALUES(?,?, 'held', ?,?)",
                            (incident_id, row["certificate_number"], self._now(), reason),
                        )
                        held_certificates.add(row["certificate_number"])
                    already_held = self.connection.execute(
                        "SELECT 1 FROM product_holds WHERE serial_number=? AND state='held'",
                        (row["serial_number"],),
                    ).fetchone()
                    if already_held is None:
                        self.connection.execute(
                            "INSERT INTO product_holds"
                            "(serial_number,incident_id,state,reason,held_at) "
                            "VALUES(?,?,'held',?,?)",
                            (row["serial_number"], incident_id, reason, self._now()),
                        )
                        held_serials.append(row["serial_number"])
                else:
                    delivered_serials.append(row["serial_number"])
            self._audit("instrument", instrument_id, "calibration.failed", actor_id,
                        {"incident_id": incident_id, "reason": reason,
                         "affected_certificates": sorted(affected_certificates),
                         "held_certificates": sorted(held_certificates),
                         "held_serials": sorted(set(held_serials)),
                         "delivered_serials": sorted(set(delivered_serials))})
        return {
            "incident_id": incident_id,
            "instrument_id": instrument_id,
            "affected_certificates": sorted(affected_certificates),
            "held_certificates": sorted(held_certificates),
            "held_serials": sorted(set(held_serials)),
            "delivered_serials": sorted(set(delivered_serials)),
        }

    def release_hold(self, actor_id: str, serial_number: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "hold.release")
        with transaction(self.connection, immediate=True):
            hold = self.connection.execute(
                "SELECT * FROM product_holds WHERE serial_number=? AND state='held'", (serial_number,)
            ).fetchone()
            if hold is None:
                raise NotFound(f"产品没有生效的暂停: {serial_number}")
            self.connection.execute(
                "UPDATE product_holds SET state='released',released_at=? WHERE serial_number=?",
                (self._now(), serial_number),
            )
            self.connection.execute(
                "UPDATE certification_holds SET state='released',resolved_at=? "
                "WHERE incident_id=? AND certificate_number IN "
                "(SELECT certificate_number FROM certifications WHERE serial_number=?) AND state='held'",
                (self._now(), hold["incident_id"], serial_number),
            )
            self._audit("product", serial_number, "hold.released", actor_id, {"note": note})
        return {"serial_number": serial_number, "state": "released"}

    def revoke_certificate(self, actor_id: str, certificate_number: str, reason: str) -> dict[str, Any]:
        """质量负责人撤销受影响认证（例如暂停后调查确认）。不删除、不改写历史。"""

        self._require(actor_id, "certificate.revoke")
        reason = self._require_text(reason, "reason")
        with transaction(self.connection, immediate=True):
            cert = self.connection.execute(
                "SELECT * FROM certifications WHERE certificate_number=?", (certificate_number,)
            ).fetchone()
            if cert is None:
                raise NotFound(f"认证不存在: {certificate_number}")
            if cert["state"] != "issued":
                raise InvalidState("认证已撤销")
            self.connection.execute(
                "UPDATE certifications SET state='revoked',revoked_at=?,revoke_reason=? "
                "WHERE certificate_number=?",
                (self._now(), reason, certificate_number),
            )
            self.connection.execute(
                "UPDATE certification_holds SET state='revoked',resolved_at=? "
                "WHERE certificate_number=? AND state='held'",
                (self._now(), certificate_number),
            )
            self._audit("certificate", certificate_number, "certification.revoked", actor_id,
                        {"reason": reason})
        return {"certificate_number": certificate_number, "state": "revoked"}

    # ----- 追踪与管理接口 --------------------------------------------------

    def device_lineage(self, actor_id: str, device_id: str) -> dict[str, Any]:
        """说明一台回收设备拆解出的部件及各自最终去向。"""

        self._require(actor_id, "trace.read")
        device = self.get_device(device_id)
        rows = self.connection.execute(
            "SELECT * FROM components WHERE device_id=? ORDER BY component_id", (device_id,)
        ).fetchall()
        components: list[dict[str, Any]] = []
        for component in rows:
            entry = dict(component)
            inspections = self.connection.execute(
                "SELECT inspection_id,method,instrument_id,calibration_id,result,evidence_sha256 "
                "FROM inspections WHERE component_id=? ORDER BY measured_at",
                (component["component_id"],)
            ).fetchall()
            repairs = self.connection.execute(
                "SELECT repair_id,process_code,process_version,evidence_sha256 "
                "FROM repairs WHERE component_id=? ORDER BY repaired_at",
                (component["component_id"],)
            ).fetchall()
            disposition = self.connection.execute(
                "SELECT * FROM dispositions WHERE component_id=?", (component["component_id"],)
            ).fetchone()
            entry["inspections"] = [dict(row) for row in inspections]
            entry["repairs"] = [dict(row) for row in repairs]
            entry["disposition"] = None if disposition is None else dict(disposition)
            components.append(entry)
        return {"device": device, "components": components}

    def material_destination_report(self, actor_id: str) -> dict[str, Any]:
        """管理接口：全部回收材料的最终去向汇总。"""

        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            """
            SELECT d.device_id, d.device_type, c.component_id, c.component_type, c.material,
                   c.weight_kg, dp.kind, dp.serial_number, dp.scrap_disposition, dp.destination,
                   dp.rule_set_id, dp.rule_version, dp.evidence_sha256
            FROM components c
            JOIN recovered_devices d ON d.device_id = c.device_id
            LEFT JOIN dispositions dp ON dp.component_id = c.component_id
            ORDER BY d.device_id, c.component_id
            """
        ).fetchall()
        items = [dict(row) for row in rows]
        reused = [item for item in items if item["kind"] == "reuse"]
        scrapped = [item for item in items if item["kind"] == "scrap"]
        open_items = [item for item in items if item["kind"] is None]
        total_weight = sum(float(item["weight_kg"] or 0) for item in items)
        settled_weight = sum(float(item["weight_kg"] or 0) for item in items if item["kind"] is not None)
        return {
            "total_components": len(items),
            "reused_count": len(reused),
            "scrapped_count": len(scrapped),
            "open_count": len(open_items),
            "recovered_weight_kg": f"{total_weight:.3f}",
            "settled_weight_kg": f"{settled_weight:.3f}",
            "items": items,
        }

    def certificate_evidence(self, actor_id: str, certificate_number: str) -> dict[str, Any]:
        """每次复用决定引用的证据版本：规则版本、检测方法/校准、修复工艺版本。"""

        self._require(actor_id, "trace.read")
        cert = self.connection.execute(
            "SELECT * FROM certifications WHERE certificate_number=?", (certificate_number,)
        ).fetchone()
        if cert is None:
            raise NotFound(f"认证不存在: {certificate_number}")
        basis = json.loads(cert["basis_json"])
        rule_version = self.connection.execute(
            "SELECT title,canonical_json,content_sha256,published_at FROM reuse_rule_versions "
            "WHERE rule_set_id=? AND version=?",
            (cert["rule_set_id"], cert["rule_version"]),
        ).fetchone()
        process_versions: dict[str, dict[str, Any]] = {}
        for component in basis["components"]:
            for repair in component["repairs"]:
                key = f"{repair['process_code']}@{repair['process_version']}"
                if key in process_versions:
                    continue
                spec = self.connection.execute(
                    "SELECT title,canonical_json,content_sha256,published_at,state "
                    "FROM process_spec_versions WHERE process_code=? AND version=?",
                    (repair["process_code"], repair["process_version"]),
                ).fetchone()
                process_versions[key] = dict(spec) if spec is not None else None
        return {
            "certificate": {
                "certificate_number": cert["certificate_number"],
                "serial_number": cert["serial_number"],
                "decision": cert["decision"],
                "state": cert["state"],
                "rule": f"{cert['rule_set_id']}@{cert['rule_version']}",
                "evidence_sha256": cert["evidence_sha256"],
                "issued_at": cert["issued_at"],
                "issued_by": cert["issued_by"],
                "revoked_at": cert["revoked_at"],
                "revoke_reason": cert["revoke_reason"],
            },
            "rule_version": dict(rule_version) if rule_version is not None else None,
            "components": basis["components"],
            "process_versions": process_versions,
        }

    def audit_trail(self, actor_id: str, entity_type: str | None = None, entity_id: str | None = None) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        query = "SELECT * FROM audit_events"
        params: list[Any] = []
        clauses: list[str] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_id"
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
