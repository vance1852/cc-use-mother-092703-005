"""再制造追踪与再认证的事务用例。

核心不变量：
1. 拆解谱系：回收设备 -> 拆解事件 -> 部件，部件状态机单向推进；
2. 工艺规范/复用规则只追加新版本；已签发认证把证据（含规范/规则版本与摘要）
   整体快照钉住，规范更新不会追溯改写认证；
3. 部件去向只有 ``new_product`` 或 ``scrap`` 两种终态，且必须通过当前生效
   复用规则校验，每个部件只能有一条去向；
4. 检测设备校准失效时，沿 检测 -> 部件 -> 认证批次 反向定位影响面，
   暂停尚未交付的产品，已交付产品只统计不改动。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "intake": {"device.write"},
    "dismantler": {"disassembly.write"},
    "inspector": {"equipment.write", "inspection.write"},
    "engineer": {"spec.write", "policy.write", "repair.write", "product.write"},
    "certifier": {"certification.write"},
    "logistics": {"scrap.write", "product.deliver"},
    "auditor": {"report.read", "audit.read"},
}

COMPONENT_STATES = ("recovered", "inspected", "repaired", "qualified", "reused", "scrapped")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def weight_text(raw: object, field: str) -> str:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed(f"{field} 必须是数字重量") from exc
    if value < 0:
        raise ValidationFailed(f"{field} 不能为负")
    return format(value, "f")


class RemanufactureService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ------------------------------------------------------------------ 用户

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reman_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reman_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------ 审计

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM reman_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO reman_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM reman_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    # -------------------------------------------------------- 回收设备与拆解

    def register_device(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "device.write")
        required = ("device_id", "device_type", "model", "serial_no", "source", "recovered_weight_kg", "recovered_at")
        self._require_fields(raw, required)
        parse_utc(str(raw["recovered_at"]), "recovered_at")
        weight = weight_text(raw["recovered_weight_kg"], "recovered_weight_kg")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO recovered_devices(device_id,device_type,model,serial_no,source,"
                    "recovered_weight_kg,recovered_at,registered_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(raw["device_id"]).strip(),
                        str(raw["device_type"]).strip(),
                        str(raw["model"]).strip(),
                        str(raw["serial_no"]).strip(),
                        str(raw["source"]).strip(),
                        weight,
                        str(raw["recovered_at"]),
                        actor_id,
                        now,
                    ),
                )
                self._audit("device", str(raw["device_id"]), "device.registered", actor_id,
                            {"device_type": raw["device_type"], "recovered_weight_kg": weight})
        except sqlite3.IntegrityError as exc:
            raise Conflict("回收设备编号或同型号序列号已存在") from exc
        return self.device(str(raw["device_id"]))

    @staticmethod
    def _require_fields(raw: Mapping[str, Any], fields: Iterable[str]) -> None:
        missing = [field for field in fields if field not in raw or raw[field] in (None, "")]
        if missing:
            raise ValidationFailed(f"缺少必填字段: {', '.join(missing)}")

    def device(self, device_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM recovered_devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            raise NotFound("回收设备不存在")
        return dict(row)

    def disassemble(
        self,
        actor_id: str,
        disassembly_id: str,
        device_id: str,
        dismantled_at: str,
        components: list[Mapping[str, Any]],
        residue_weight_kg: object = "0",
        residue_destination: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """登记拆解并建立设备 -> 部件谱系。部件重量之和与残余合计需与回收重量平衡。"""
        self._require(actor_id, "disassembly.write")
        parse_utc(dismantled_at, "dismantled_at")
        if not components:
            raise ValidationFailed("拆解至少要登记一个部件")
        device = self.device(device_id)
        if device["state"] != "registered":
            raise InvalidState("设备已拆解，不能重复建立谱系")
        now = self._now()
        parsed_parts: list[tuple[str, str, str, str, Decimal]] = []
        total = Decimal("0")
        for index, item in enumerate(components, start=1):
            self._require_fields(item, ("component_id", "component_type", "name", "weight_kg"))
            weight = Decimal(weight_text(item["weight_kg"], "weight_kg"))
            if weight <= 0:
                raise ValidationFailed("部件重量必须为正")
            total += weight
            parsed_parts.append(
                (
                    str(item["component_id"]).strip(),
                    str(item["component_type"]).strip(),
                    str(item["name"]).strip(),
                    str(item.get("material", "")).strip(),
                    weight,
                )
            )
        residue = Decimal(weight_text(residue_weight_kg, "residue_weight_kg"))
        recovered = Decimal(device["recovered_weight_kg"])
        if total + residue != recovered:
            raise Conflict(
                f"重量不平衡：部件 {total} + 残余 {residue} != 回收重量 {recovered}"
            )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO disassemblies(disassembly_id,device_id,dismantled_at,residue_weight_kg,"
                    "residue_destination,note,dismantled_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (disassembly_id, device_id, dismantled_at, format(residue, "f"),
                     residue_destination, note, actor_id, now),
                )
                for index, (component_id, component_type, name, material, weight) in enumerate(parsed_parts, start=1):
                    self.connection.execute(
                        "INSERT INTO components(component_id,device_id,disassembly_id,component_type,"
                        "name,material,weight_kg,sequence,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (component_id, device_id, disassembly_id, component_type, name,
                         material, format(weight, "f"), index, now),
                    )
                self.connection.execute(
                    "UPDATE recovered_devices SET state='dismantled' WHERE device_id=? AND state='registered'",
                    (device_id,),
                )
                self._audit("device", device_id, "device.dismantled", actor_id,
                            {"disassembly_id": disassembly_id, "components": len(parsed_parts)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("拆解编号或部件编号冲突") from exc
        return self.genealogy(device_id)

    def genealogy(self, device_id: str) -> dict[str, Any]:
        """返回回收设备的完整拆解谱系。"""
        device = self.device(device_id)
        disassembly = self.connection.execute(
            "SELECT * FROM disassemblies WHERE device_id=?", (device_id,)
        ).fetchone()
        if disassembly is None:
            return {"device": device, "disassembly": None, "components": []}
        rows = self.connection.execute(
            "SELECT * FROM components WHERE device_id=? ORDER BY sequence", (device_id,)
        ).fetchall()
        components = []
        for row in rows:
            item = dict(row)
            item["disposition"] = self._disposition_of(row["component_id"])
            components.append(item)
        return {"device": device, "disassembly": dict(disassembly), "components": components}

    def _component(self, component_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if row is None:
            raise NotFound("部件不存在")
        return row

    def _advance_component(self, component_id: str, allowed_from: tuple[str, ...], new_state: str) -> None:
        cursor = self.connection.execute(
            "UPDATE components SET state=? WHERE component_id=? AND state IN (%s)"
            % ",".join("?" for _ in allowed_from),
            (new_state, component_id, *allowed_from),
        )
        if cursor.rowcount != 1:
            row = self._component(component_id)
            raise InvalidState(f"部件当前状态 {row['state']} 不能推进到 {new_state}")

    # ------------------------------------------------------------ 检测与校准

    def register_equipment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "equipment.write")
        self._require_fields(raw, ("equipment_id", "name", "serial_no", "calibration_valid_from", "calibration_valid_to"))
        valid_from = parse_utc(str(raw["calibration_valid_from"]), "calibration_valid_from")
        valid_to = parse_utc(str(raw["calibration_valid_to"]), "calibration_valid_to")
        if valid_to <= valid_from:
            raise ValidationFailed("校准有效期止必须晚于起")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inspection_equipment(equipment_id,name,serial_no,calibration_valid_from,"
                    "calibration_valid_to,registered_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(raw["equipment_id"]).strip(), str(raw["name"]).strip(),
                     str(raw["serial_no"]).strip(), utc_text(valid_from), utc_text(valid_to), actor_id, now),
                )
                self.connection.execute(
                    "INSERT INTO calibration_events(equipment_id,calibrated_at,valid_from,valid_to,"
                    "certificate_ref,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(raw["equipment_id"]).strip(), now, utc_text(valid_from), utc_text(valid_to),
                     str(raw.get("certificate_ref", "初始校准")), actor_id, now),
                )
                self._audit("equipment", str(raw["equipment_id"]), "equipment.registered", actor_id,
                            {"serial_no": raw["serial_no"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("检测设备编号已存在") from exc
        return self.equipment(str(raw["equipment_id"]))

    def equipment(self, equipment_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM inspection_equipment WHERE equipment_id=?", (equipment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("检测设备不存在")
        return dict(row)

    def recalibrate(
        self,
        actor_id: str,
        equipment_id: str,
        calibrated_at: str,
        valid_from: str,
        valid_to: str,
        certificate_ref: str,
    ) -> dict[str, Any]:
        """登记新校准。只更新设备有效期；不改变既有检测记录。"""
        self._require(actor_id, "equipment.write")
        self.equipment(equipment_id)
        start = parse_utc(valid_from, "valid_from")
        end = parse_utc(valid_to, "valid_to")
        if end <= start:
            raise ValidationFailed("校准有效期止必须晚于起")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO calibration_events(equipment_id,calibrated_at,valid_from,valid_to,"
                "certificate_ref,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (equipment_id, calibrated_at, utc_text(start), utc_text(end), certificate_ref, actor_id, now),
            )
            # 重新校准会刷新有效期，但设备处于校准失效事件期间时仍保持 incident，
            # 必须经复核关闭事件后才能重新用于检测。
            self.connection.execute(
                "UPDATE inspection_equipment SET calibration_valid_from=?,calibration_valid_to=? "
                "WHERE equipment_id=?",
                (utc_text(start), utc_text(end), equipment_id),
            )
            self._audit("equipment", equipment_id, "equipment.recalibrated", actor_id,
                        {"valid_from": utc_text(start), "valid_to": utc_text(end)})
        return self.equipment(equipment_id)

    def record_inspection(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记部件检测：方法、设备、判定准则、实测值与结论。检测时设备必须在校准有效期内。"""
        self._require(actor_id, "inspection.write")
        self._require_fields(raw, ("inspection_id", "component_id", "method_code", "method_name",
                                  "equipment_id", "inspected_at", "result"))
        result = str(raw["result"])
        if result not in ("pass", "fail"):
            raise ValidationFailed("result 必须是 pass 或 fail")
        inspected_at = parse_utc(str(raw["inspected_at"]), "inspected_at")
        component = self._component(str(raw["component_id"]))
        if component["state"] not in ("recovered", "repaired"):
            raise InvalidState(f"部件状态 {component['state']} 不能再登记检测")
        equipment = self.equipment(str(raw["equipment_id"]))
        if equipment["state"] != "valid":
            raise InvalidState("检测设备处于校准失效状态，不能用于检测")
        inspected_text = utc_text(inspected_at)
        if not (equipment["calibration_valid_from"] <= inspected_text <= equipment["calibration_valid_to"]):
            raise InvalidState("检测时间不在设备校准有效期内")
        measured = raw.get("measured", {})
        criteria = raw.get("criteria", {})
        if not isinstance(measured, Mapping) or not isinstance(criteria, Mapping):
            raise ValidationFailed("measured 与 criteria 必须是对象")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inspection_records(inspection_id,component_id,method_code,method_name,"
                    "equipment_id,inspected_at,result,measured_json,criteria_json,inspector,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (str(raw["inspection_id"]).strip(), component["component_id"], str(raw["method_code"]).strip(),
                     str(raw["method_name"]).strip(), equipment["equipment_id"], inspected_text, result,
                     canonical_json(measured), canonical_json(criteria), actor_id, now),
                )
                self._advance_component(component["component_id"], ("recovered", "repaired"), "inspected")
                self._audit("component", component["component_id"], "inspection.recorded", actor_id,
                            {"inspection_id": raw["inspection_id"], "result": result,
                             "method_code": raw["method_code"], "equipment_id": equipment["equipment_id"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("检测记录编号冲突") from exc
        return self.inspection(str(raw["inspection_id"]))

    def inspection(self, inspection_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM inspection_records WHERE inspection_id=?", (inspection_id,)
        ).fetchone()
        if row is None:
            raise NotFound("检测记录不存在")
        item = dict(row)
        item["measured"] = json.loads(item.pop("measured_json"))
        item["criteria"] = json.loads(item.pop("criteria_json"))
        return item

    # ------------------------------------------------------- 规范与复用规则

    def publish_process_spec(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """发布工艺规范新版本。内容哈希变化才允许出新版本，旧版本保留可追溯。"""
        self._require(actor_id, "spec.write")
        self._require_fields(raw, ("spec_id", "title", "content"))
        if not isinstance(raw["content"], Mapping):
            raise ValidationFailed("content 必须是对象")
        spec_id = str(raw["spec_id"]).strip()
        content = dict(raw["content"])
        content_sha = digest(content)
        latest = self.connection.execute(
            "SELECT version,content_sha256,state FROM process_specs WHERE spec_id=? ORDER BY version DESC LIMIT 1",
            (spec_id,),
        ).fetchone()
        if latest is not None and latest["content_sha256"] == content_sha:
            raise Conflict("规范内容未变化，不能重复发布版本")
        if latest is not None and latest["state"] != "active":
            raise InvalidState("该规范系列已停用")
        version = 1 if latest is None else int(latest["version"]) + 1
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO process_specs(spec_id,version,title,content_json,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (spec_id, version, str(raw["title"]).strip(), canonical_json(content), content_sha, actor_id, now),
            )
            self._audit("process_spec", f"{spec_id}@v{version}", "spec.published", actor_id,
                        {"spec_id": spec_id, "version": version, "sha256": content_sha})
        return {"spec_id": spec_id, "version": version, "content_sha256": content_sha, "state": "active"}

    def process_spec(self, spec_id: str, version: int | None = None) -> dict[str, Any]:
        row = self._versioned_row("process_specs", spec_id, version)
        return {
            "spec_id": row["spec_id"],
            "version": row["version"],
            "title": row["title"],
            "content": json.loads(row["content_json"]),
            "content_sha256": row["content_sha256"],
            "state": row["state"],
            "created_at": row["created_at"],
        }

    def retire_process_spec(self, actor_id: str, spec_id: str) -> dict[str, Any]:
        self._require(actor_id, "spec.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE process_specs SET state='retired' WHERE spec_id=? AND state='active'",
                (spec_id,),
            )
            if cursor.rowcount == 0:
                raise NotFound("没有生效中的工艺规范版本")
            self._audit("process_spec", spec_id, "spec.retired", actor_id, {"versions": cursor.rowcount})
        return {"spec_id": spec_id, "state": "retired"}

    def publish_reuse_policy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """发布复用规则新版本。

        规则形如 ``{"iron_core": {"allow_new_products": ["distribution-transformer"],
        "allow_scrap": false}, "copper_winding": {...}}``。
        """
        self._require(actor_id, "policy.write")
        self._require_fields(raw, ("policy_id", "title", "rules"))
        rules = raw["rules"]
        normalized = self._normalize_rules(rules)
        policy_id = str(raw["policy_id"]).strip()
        content_sha = digest(normalized)
        latest = self.connection.execute(
            "SELECT version,content_sha256,state FROM reuse_policies WHERE policy_id=? ORDER BY version DESC LIMIT 1",
            (policy_id,),
        ).fetchone()
        if latest is not None and latest["content_sha256"] == content_sha:
            raise Conflict("复用规则内容未变化，不能重复发布版本")
        if latest is not None and latest["state"] != "active":
            raise InvalidState("该规则系列已停用")
        version = 1 if latest is None else int(latest["version"]) + 1
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO reuse_policies(policy_id,version,title,rules_json,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (policy_id, version, str(raw["title"]).strip(), canonical_json(normalized),
                 content_sha, actor_id, now),
            )
            self._audit("reuse_policy", f"{policy_id}@v{version}", "policy.published", actor_id,
                        {"policy_id": policy_id, "version": version, "sha256": content_sha})
        return {"policy_id": policy_id, "version": version, "content_sha256": content_sha, "state": "active"}

    @staticmethod
    def _normalize_rules(rules: object) -> dict[str, Any]:
        if not isinstance(rules, Mapping):
            raise ValidationFailed("rules 必须是部件类型到规则的映射")
        normalized: dict[str, Any] = {}
        for component_type, rule in rules.items():
            key = str(component_type).strip()
            if not key or not isinstance(rule, Mapping):
                raise ValidationFailed(f"规则 {component_type} 必须是对象")
            allow_new = rule.get("allow_new_products", [])
            allow_scrap = bool(rule.get("allow_scrap", False))
            if not isinstance(allow_new, list) or not all(isinstance(item, str) and item.strip() for item in allow_new):
                raise ValidationFailed(f"规则 {key} 的 allow_new_products 必须是非空字符串数组")
            if not allow_new and not allow_scrap:
                raise ValidationFailed(f"规则 {key} 至少要允许一种去向（新产品或报废）")
            normalized[key] = {
                "allow_new_products": sorted(str(item).strip() for item in allow_new),
                "allow_scrap": allow_scrap,
            }
        if not normalized:
            raise ValidationFailed("复用规则不能为空")
        return normalized

    def reuse_policy(self, policy_id: str, version: int | None = None) -> dict[str, Any]:
        row = self._versioned_row("reuse_policies", policy_id, version)
        return {
            "policy_id": row["policy_id"],
            "version": row["version"],
            "title": row["title"],
            "rules": json.loads(row["rules_json"]),
            "content_sha256": row["content_sha256"],
            "state": row["state"],
            "created_at": row["created_at"],
        }

    def _versioned_row(self, table: str, doc_id: str, version: int | None) -> sqlite3.Row:
        if version is None:
            row = self.connection.execute(
                f"SELECT * FROM {table} WHERE { 'spec_id' if table == 'process_specs' else 'policy_id'}=? "
                "ORDER BY version DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
        else:
            column = "spec_id" if table == "process_specs" else "policy_id"
            row = self.connection.execute(
                f"SELECT * FROM {table} WHERE {column}=? AND version=?", (doc_id, version)
            ).fetchone()
        if row is None:
            raise NotFound("版本不存在" if version is not None else "文档不存在")
        return row

    def _active_policy(self, policy_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reuse_policies WHERE policy_id=? AND state='active' ORDER BY version DESC LIMIT 1",
            (policy_id,),
        ).fetchone()
        if row is None:
            raise NotFound("没有生效中的复用规则")
        return row

    def retire_reuse_policy(self, actor_id: str, policy_id: str) -> dict[str, Any]:
        self._require(actor_id, "policy.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE reuse_policies SET state='retired' WHERE policy_id=? AND state='active'",
                (policy_id,),
            )
            if cursor.rowcount == 0:
                raise NotFound("没有生效中的复用规则版本")
            self._audit("reuse_policy", policy_id, "policy.retired", actor_id, {"versions": cursor.rowcount})
        return {"policy_id": policy_id, "state": "retired"}

    def _check_destination(self, rules: Mapping[str, Any], component_type: str,
                           kind: str, product_type: str | None) -> None:
        rule = rules.get(component_type)
        if rule is None:
            raise Conflict(f"复用规则没有定义部件类型 {component_type} 的去向")
        if kind == "scrap":
            if not rule["allow_scrap"]:
                raise Conflict(f"部件类型 {component_type} 规则禁止报废")
        elif kind == "new_product":
            if product_type not in rule["allow_new_products"]:
                raise Conflict(
                    f"部件类型 {component_type} 只能进入 {rule['allow_new_products']}，"
                    f"不能进入 {product_type}"
                )
        else:  # pragma: no cover - 由数据库 CHECK 兜底
            raise ValidationFailed("未知去向类型")

    # ------------------------------------------------------------------ 修复

    def repair_component(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记修复工艺：钉住工艺规范版本与内容摘要，后续规范改版不影响本条记录。"""
        self._require(actor_id, "repair.write")
        self._require_fields(raw, ("repair_id", "component_id", "spec_id", "spec_version", "repaired_at"))
        component = self._component(str(raw["component_id"]))
        if component["state"] not in ("inspected",):
            raise InvalidState(f"部件状态 {component['state']} 不能执行修复")
        spec_row = self.connection.execute(
            "SELECT * FROM process_specs WHERE spec_id=? AND version=?",
            (str(raw["spec_id"]).strip(), int(raw["spec_version"])),
        ).fetchone()
        if spec_row is None:
            raise NotFound("引用的工艺规范版本不存在")
        parameters = raw.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ValidationFailed("parameters 必须是对象")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO repairs(repair_id,component_id,spec_id,spec_version,spec_sha256,"
                    "parameters_json,repaired_at,repaired_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(raw["repair_id"]).strip(), component["component_id"], spec_row["spec_id"],
                     spec_row["version"], spec_row["content_sha256"], canonical_json(parameters),
                     str(raw["repaired_at"]), actor_id, now),
                )
                self._advance_component(component["component_id"], ("inspected",), "repaired")
                self._audit("component", component["component_id"], "repair.recorded", actor_id,
                            {"repair_id": raw["repair_id"], "spec_id": spec_row["spec_id"],
                             "spec_version": spec_row["version"], "spec_sha256": spec_row["content_sha256"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("修复记录编号冲突") from exc
        return self.repair(str(raw["repair_id"]))

    def repair(self, repair_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM repairs WHERE repair_id=?", (repair_id,)).fetchone()
        if row is None:
            raise NotFound("修复记录不存在")
        item = dict(row)
        item["parameters"] = json.loads(item.pop("parameters_json"))
        return item

    # ---------------------------------------------------------------- 再认证

    def issue_certification(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """签发再认证决定。

        要求每个部件：当前状态允许（检测合格/修复后）、最新检测为 pass、
        未被其他认证占用、新产品类型符合当前生效复用规则。证据（检测、修复、
        规范/规则版本与摘要）整体快照并计算摘要，认证后不可变。
        """
        self._require(actor_id, "certification.write")
        self._require_fields(raw, ("certification_id", "batch_no", "product_type",
                                   "component_ids", "policy_id"))
        component_ids = raw["component_ids"]
        if not isinstance(component_ids, list) or not component_ids:
            raise ValidationFailed("component_ids 必须是非空数组")
        if len(set(component_ids)) != len(component_ids):
            raise ValidationFailed("认证部件列表存在重复")
        product_type = str(raw["product_type"]).strip()
        policy = self._active_policy(str(raw["policy_id"]))
        rules = json.loads(policy["rules_json"])
        now = self._now()
        evidence_components: list[dict[str, Any]] = []
        component_rows: list[sqlite3.Row] = []
        with transaction(self.connection, immediate=True):
            for component_id in component_ids:
                component = self._component(str(component_id))
                if component["state"] not in ("inspected", "repaired", "qualified"):
                    raise InvalidState(
                        f"部件 {component_id} 状态 {component['state']}，未完成合格检测，不能认证"
                    )
                occupied = self.connection.execute(
                    "SELECT certification_id FROM certification_components WHERE component_id=?",
                    (component_id,),
                ).fetchone()
                if occupied is not None:
                    raise Conflict(f"部件 {component_id} 已属于认证批次 {occupied['certification_id']}")
                self._check_destination(rules, component["component_type"], "new_product", product_type)
                latest = self.connection.execute(
                    "SELECT * FROM inspection_records WHERE component_id=? "
                    "ORDER BY inspected_at DESC,rowid DESC LIMIT 1",
                    (component_id,),
                ).fetchone()
                if latest is None:
                    raise InvalidState(f"部件 {component_id} 没有检测记录")
                if latest["result"] != "pass" or latest["state"] != "valid":
                    raise InvalidState(f"部件 {component_id} 最新检测未通过或已受校准嫌疑标记")
                repair_row = self.connection.execute(
                    "SELECT * FROM repairs WHERE component_id=? ORDER BY repaired_at DESC,rowid DESC LIMIT 1",
                    (component_id,),
                ).fetchone()
                evidence_components.append({
                    "component_id": component["component_id"],
                    "component_type": component["component_type"],
                    "weight_kg": component["weight_kg"],
                    "inspection": {
                        "inspection_id": latest["inspection_id"],
                        "method_code": latest["method_code"],
                        "method_name": latest["method_name"],
                        "equipment_id": latest["equipment_id"],
                        "inspected_at": latest["inspected_at"],
                        "result": latest["result"],
                        "measured": json.loads(latest["measured_json"]),
                        "criteria": json.loads(latest["criteria_json"]),
                    },
                    "repair": None if repair_row is None else {
                        "repair_id": repair_row["repair_id"],
                        "spec_id": repair_row["spec_id"],
                        "spec_version": repair_row["spec_version"],
                        "spec_sha256": repair_row["spec_sha256"],
                        "parameters": json.loads(repair_row["parameters_json"]),
                    },
                })
                component_rows.append(component)
            evidence = {
                "batch_no": str(raw["batch_no"]).strip(),
                "product_type": product_type,
                "policy": {"policy_id": policy["policy_id"], "version": policy["version"],
                           "sha256": policy["content_sha256"], "rules": rules},
                "components": evidence_components,
                "decided_at": now,
            }
            evidence_sha = digest(evidence)
            try:
                self.connection.execute(
                    "INSERT INTO certifications(certification_id,batch_no,product_type,policy_id,policy_version,"
                    "policy_sha256,evidence_json,evidence_sha256,decided_by,decided_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (str(raw["certification_id"]).strip(), str(raw["batch_no"]).strip(), product_type,
                     policy["policy_id"], policy["version"], policy["content_sha256"],
                     canonical_json(evidence), evidence_sha, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("认证编号或批次号冲突") from exc
            for component in component_rows:
                self.connection.execute(
                    "INSERT INTO certification_components(certification_id,component_id) VALUES(?,?)",
                    (str(raw["certification_id"]).strip(), component["component_id"]),
                )
                self.connection.execute(
                    "UPDATE components SET state='qualified' WHERE component_id=? AND state IN ('inspected','repaired')",
                    (component["component_id"],),
                )
            self.connection.execute(
                "INSERT INTO certification_events(certification_id,action,reason,actor_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (str(raw["certification_id"]).strip(), "issued", str(raw.get("note", "")), actor_id, now),
            )
            self._audit("certification", str(raw["certification_id"]), "certification.issued", actor_id,
                        {"batch_no": raw["batch_no"], "product_type": product_type,
                         "policy": f"{policy['policy_id']}@v{policy['version']}",
                         "evidence_sha256": evidence_sha, "components": len(component_rows)})
        return self.certification(str(raw["certification_id"]))

    def certification(self, certification_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certifications WHERE certification_id=?", (certification_id,)
        ).fetchone()
        if row is None:
            raise NotFound("再认证批次不存在")
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        components = self.connection.execute(
            "SELECT component_id FROM certification_components WHERE certification_id=? ORDER BY component_id",
            (certification_id,),
        ).fetchall()
        item["component_ids"] = [row["component_id"] for row in components]
        events = self.connection.execute(
            "SELECT action,reason,incident_id,actor_id,created_at FROM certification_events "
            "WHERE certification_id=? ORDER BY event_id",
            (certification_id,),
        ).fetchall()
        item["events"] = [dict(row) for row in events]
        return item

    # ----------------------------------------------------------- 新产品与去向

    def build_product(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """用已认证部件装成新产品，复用决定钉住认证时的规则证据版本。"""
        self._require(actor_id, "product.write")
        self._require_fields(raw, ("product_id", "certification_id", "built_at"))
        certification_id = str(raw["certification_id"]).strip()
        cert = self.connection.execute(
            "SELECT * FROM certifications WHERE certification_id=?", (certification_id,)
        ).fetchone()
        if cert is None:
            raise NotFound("再认证批次不存在")
        if cert["decision"] != "issued" or cert["state"] not in ("issued", "released"):
            raise InvalidState(f"认证批次状态 {cert['state']}，不能用于生产")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO products(product_id,product_type,certification_id,built_at,built_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(raw["product_id"]).strip(), cert["product_type"], certification_id,
                     str(raw["built_at"]), actor_id, now),
                )
                self._audit("product", str(raw["product_id"]), "product.built", actor_id,
                            {"certification_id": certification_id, "product_type": cert["product_type"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("产品编号冲突") from exc
        return self.product(str(raw["product_id"]))

    def product(self, product_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
        if row is None:
            raise NotFound("产品不存在")
        return dict(row)

    def _disposition_of(self, component_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM dispositions WHERE component_id=?", (component_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def assign_to_product(self, actor_id: str, product_id: str, component_ids: list[str],
                          idempotency_key: str | None = None) -> dict[str, Any]:
        """把认证部件的最终去向登记为新产品；去向引用认证快照中的规则证据版本。"""
        self._require(actor_id, "product.write")
        return self._create_dispositions(
            actor_id, "new_product", component_ids, product_id=product_id,
            idempotency_key=idempotency_key,
        )

    def scrap_component(
        self,
        actor_id: str,
        disposition_id: str,
        component_id: str,
        destination: str,
        policy_id: str,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """把部件的最终去向登记为报废，须通过当前生效复用规则的报废许可。"""
        self._require(actor_id, "scrap.write")
        return self._create_dispositions(
            actor_id, "scrap", [component_id], destination=destination,
            policy_id=policy_id, disposition_id=disposition_id, note=note,
            idempotency_key=idempotency_key,
        )

    def _create_dispositions(
        self,
        actor_id: str,
        kind: str,
        component_ids: list[str],
        product_id: str | None = None,
        destination: str | None = None,
        policy_id: str | None = None,
        disposition_id: str | None = None,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not component_ids:
            raise ValidationFailed("至少登记一个部件的去向")
        if idempotency_key:
            stored = self.connection.execute(
                "SELECT request_sha256,response_json FROM reman_idempotency "
                "WHERE scope='disposition' AND idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            request_digest = digest({"kind": kind, "component_ids": component_ids,
                                     "product_id": product_id, "destination": destination})
            if stored is not None:
                if stored["request_sha256"] != request_digest:
                    raise Conflict("幂等键对应不同去向内容")
                return json.loads(stored["response_json"])
        else:
            request_digest = None
        product_row = None
        if kind == "new_product":
            product_row = self.product(product_id)  # type: ignore[arg-type]
            if product_row["state"] != "built":
                raise InvalidState(f"产品状态 {product_row['state']}，不能再分配部件")
            cert_row = self.connection.execute(
                "SELECT policy_id,policy_version,policy_sha256,evidence_json FROM certifications "
                "WHERE certification_id=?",
                (product_row["certification_id"],),
            ).fetchone()
            policy_ref = {"policy_id": cert_row["policy_id"], "version": cert_row["policy_version"],
                          "sha256": cert_row["policy_sha256"]}
            rules = json.loads(cert_row["evidence_json"])["policy"]["rules"]
        else:
            active = self._active_policy(policy_id)  # type: ignore[arg-type]
            policy_ref = {"policy_id": active["policy_id"], "version": active["version"],
                          "sha256": active["content_sha256"]}
            rules = json.loads(active["rules_json"])
        created: list[dict[str, Any]] = []
        now = self._now()
        with transaction(self.connection, immediate=True):
            for index, component_id in enumerate(component_ids):
                component = self._component(str(component_id))
                self._check_destination(rules, component["component_type"], kind,
                                        None if product_row is None else product_row["product_type"])
                if kind == "new_product":
                    membership = self.connection.execute(
                        "SELECT 1 FROM certification_components WHERE certification_id=? AND component_id=?",
                        (product_row["certification_id"], component["component_id"]),
                    ).fetchone()
                    if membership is None:
                        raise Conflict(f"部件 {component_id} 不属于该产品的认证批次，不能装配")
                    if component["state"] != "qualified":
                        raise InvalidState(f"部件 {component_id} 状态 {component['state']}，尚未取得再认证")
                    target_destination = f"product:{product_id}"
                    did = f"disp-{product_id}-{component['component_id']}"
                    cert_for_disp = product_row["certification_id"]
                else:
                    if component["state"] in ("qualified", "reused", "scrapped"):
                        raise Conflict(
                            f"部件 {component_id} 状态 {component['state']}，已有终态去向或已纳入认证批次"
                        )
                    target_destination = str(destination).strip()
                    if not target_destination:
                        raise ValidationFailed("报废目的地不能为空")
                    did = str(disposition_id).strip() if len(component_ids) == 1 else f"{disposition_id}-{index}"
                    cert_for_disp = None
                try:
                    self.connection.execute(
                        "INSERT INTO dispositions(disposition_id,component_id,kind,product_id,destination,"
                        "weight_kg,policy_id,policy_version,policy_sha256,certification_id,note,decided_by,"
                        "decided_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (did, component["component_id"], kind,
                         product_id if kind == "new_product" else None,
                         target_destination, component["weight_kg"],
                         policy_ref["policy_id"], policy_ref["version"], policy_ref["sha256"],
                         cert_for_disp, note, actor_id, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"部件 {component_id} 已有去向记录或去向编号冲突") from exc
                self.connection.execute(
                    f"UPDATE components SET state='{'reused' if kind == 'new_product' else 'scrapped'}' "
                    "WHERE component_id=?",
                    (component["component_id"],),
                )
                created.append(self._disposition_of(component["component_id"]) or {})
                self._audit("component", component["component_id"],
                            "disposition.new_product" if kind == "new_product" else "disposition.scrap",
                            actor_id,
                            {"disposition_id": did, "destination": target_destination,
                             "policy": f"{policy_ref['policy_id']}@v{policy_ref['version']}",
                             "policy_sha256": policy_ref["sha256"]})
            if kind == "new_product":
                self._audit("product", product_id, "product.components_assigned", actor_id,
                            {"components": len(component_ids)})
            response = {"kind": kind, "count": len(created), "dispositions": created}
            if idempotency_key:
                self.connection.execute(
                    "INSERT INTO reman_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('disposition',?,?,?,?)",
                    (idempotency_key, request_digest, canonical_json(response), now),
                )
        return response

    def deliver_product(self, actor_id: str, product_id: str, delivered_at: str, customer: str) -> dict[str, Any]:
        self._require(actor_id, "product.deliver")
        parse_utc(delivered_at, "delivered_at")
        product = self.product(product_id)
        if product["state"] == "delivered":
            raise InvalidState("产品已交付")
        if product["state"] == "suspended":
            raise InvalidState("产品因认证嫌疑被暂停，不能交付")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE products SET state='delivered',delivered_at=?,customer=? WHERE product_id=? AND state='built'",
                (delivered_at, customer, product_id),
            )
            self._audit("product", product_id, "product.delivered", actor_id, {"customer": customer})
        return self.product(product_id)

    # ------------------------------------------------------- 校准失效影响处置

    def report_calibration_incident(
        self,
        actor_id: str,
        incident_id: str,
        equipment_id: str,
        invalid_from: str,
        reason: str,
        detected_at: str | None = None,
    ) -> dict[str, Any]:
        """登记校准失效：标记嫌疑检测、定位认证批次、暂停未交付产品。

        影响时间窗为 [invalid_from, 发现时刻)。期间使用该设备的合格检测成为
        嫌疑记录；对应部件所在的已签发认证批次被定位，批次下未交付产品暂停。
        """
        self._require(actor_id, "equipment.write")
        equipment = self.equipment(equipment_id)
        start_text = utc_text(parse_utc(invalid_from, "invalid_from"))
        now = self._now()
        detected_text = now if detected_at is None else utc_text(parse_utc(detected_at, "detected_at"))
        if detected_text < start_text:
            raise ValidationFailed("发现时间不能早于失效起始时间")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO calibration_incidents(incident_id,equipment_id,invalid_from,detected_at,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (incident_id, equipment_id, start_text, detected_text, reason, actor_id, now),
                )
                suspect = self.connection.execute(
                    "SELECT inspection_id,component_id FROM inspection_records "
                    "WHERE equipment_id=? AND inspected_at>=? AND inspected_at<? AND state='valid'",
                    (equipment_id, start_text, detected_text),
                ).fetchall()
                suspect_ids = [row["inspection_id"] for row in suspect]
                affected_components = sorted({row["component_id"] for row in suspect})
                if suspect_ids:
                    self.connection.execute(
                        f"UPDATE inspection_records SET state='suspect' WHERE inspection_id IN "
                        f"({','.join('?' for _ in suspect_ids)})",
                        suspect_ids,
                    )
                impacted = self.connection.execute(
                    "SELECT cc.certification_id,count(*) AS parts FROM certification_components cc "
                    f"WHERE cc.component_id IN ({','.join('?' for _ in affected_components)}) "
                    "GROUP BY cc.certification_id",
                    affected_components or [""],
                ).fetchall() if affected_components else []
                impact_rows: list[dict[str, Any]] = []
                for row in impacted:
                    cert = self.connection.execute(
                        "SELECT * FROM certifications WHERE certification_id=?",
                        (row["certification_id"],),
                    ).fetchone()
                    if cert["decision"] != "issued":
                        continue
                    product_counts = self.connection.execute(
                        "SELECT state,count(*) AS n FROM products WHERE certification_id=? GROUP BY state",
                        (cert["certification_id"],),
                    ).fetchall()
                    counts = {item["state"]: item["n"] for item in product_counts}
                    undelivered = counts.get("built", 0) + counts.get("suspended", 0)
                    delivered = counts.get("delivered", 0)
                    self.connection.execute(
                        "INSERT INTO incident_impacts(incident_id,certification_id,suspended_product_count,"
                        "delivered_product_count) VALUES(?,?,?,?)",
                        (incident_id, cert["certification_id"], undelivered, delivered),
                    )
                    self.connection.execute(
                        "UPDATE products SET state='suspended' WHERE certification_id=? AND state='built'",
                        (cert["certification_id"],),
                    )
                    if cert["state"] == "issued":
                        self.connection.execute(
                            "UPDATE certifications SET state='suspended' WHERE certification_id=?",
                            (cert["certification_id"],),
                        )
                        self.connection.execute(
                            "INSERT INTO certification_events(certification_id,action,reason,incident_id,"
                            "actor_id,created_at) VALUES(?,?,?,?,?,?)",
                            (cert["certification_id"], "suspended",
                             f"检测设备 {equipment_id} 校准失效：{reason}", incident_id, actor_id, now),
                        )
                    impact_rows.append({
                        "certification_id": cert["certification_id"],
                        "batch_no": cert["batch_no"],
                        "affected_component_count": row["parts"],
                        "suspended_product_count": undelivered,
                        "delivered_product_count": delivered,
                        "certification_state": "suspended" if cert["state"] == "issued" else cert["state"],
                    })
                self.connection.execute(
                    "UPDATE inspection_equipment SET state='incident' WHERE equipment_id=?",
                    (equipment_id,),
                )
                self.connection.execute(
                    "UPDATE calibration_incidents SET affected_inspection_count=?,affected_certification_count=?,"
                    "suspended_product_count=? WHERE incident_id=?",
                    (len(suspect_ids), len(impact_rows),
                     sum(item["suspended_product_count"] for item in impact_rows), incident_id),
                )
                self._audit("equipment", equipment_id, "calibration.incident_reported", actor_id,
                            {"incident_id": incident_id, "invalid_from": start_text,
                             "suspect_inspections": len(suspect_ids),
                             "affected_certifications": len(impact_rows)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("校准失效事件编号冲突") from exc
        return self.incident(incident_id)

    def incident(self, incident_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM calibration_incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if row is None:
            raise NotFound("校准失效事件不存在")
        item = dict(row)
        item["impacts"] = [
            dict(impact) for impact in self.connection.execute(
                "SELECT * FROM incident_impacts WHERE incident_id=? ORDER BY certification_id",
                (incident_id,),
            ).fetchall()
        ]
        return item

    def resolve_calibration_incident(
        self,
        actor_id: str,
        incident_id: str,
        resolution: str,
        release: bool,
    ) -> dict[str, Any]:
        """复核后关闭事件。release=True 时解除嫌疑与暂停（检测维持有效时）。

        已签发认证的证据快照永不重写；解除暂停只是状态恢复，认证仍指向原证据版本。
        """
        self._require(actor_id, "equipment.write")
        incident = self.connection.execute(
            "SELECT * FROM calibration_incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if incident is None:
            raise NotFound("校准失效事件不存在")
        if incident["state"] != "open":
            raise InvalidState("事件已关闭")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE calibration_incidents SET state='resolved',resolved_at=? WHERE incident_id=?",
                (now, incident_id),
            )
            if release:
                self.connection.execute(
                    "UPDATE inspection_records SET state='valid' WHERE equipment_id=? "
                    "AND inspected_at>=? AND inspected_at<? AND state='suspect'",
                    (incident["equipment_id"], incident["invalid_from"], incident["detected_at"]),
                )
                impacts = self.connection.execute(
                    "SELECT certification_id FROM incident_impacts WHERE incident_id=?", (incident_id,)
                ).fetchall()
                for impact in impacts:
                    cert_id = impact["certification_id"]
                    still_open = self.connection.execute(
                        "SELECT 1 FROM incident_impacts ii "
                        "JOIN calibration_incidents ci ON ci.incident_id=ii.incident_id "
                        "WHERE ii.certification_id=? AND ci.state='open' AND ii.incident_id<>? LIMIT 1",
                        (cert_id, incident_id),
                    ).fetchone()
                    if still_open is None:
                        self.connection.execute(
                            "UPDATE products SET state='built' WHERE certification_id=? AND state='suspended'",
                            (cert_id,),
                        )
                        self.connection.execute(
                            "UPDATE certifications SET state='issued' WHERE certification_id=? "
                            "AND state='suspended'",
                            (cert_id,),
                        )
                        action = "released"
                    else:
                        action = "release_deferred"
                    self.connection.execute(
                        "INSERT INTO certification_events(certification_id,action,reason,incident_id,"
                        "actor_id,created_at) VALUES(?,?,?,?,?,?)",
                        (cert_id, action, f"校准复核完成，解除暂停：{resolution}", incident_id, actor_id, now),
                    )
                self.connection.execute(
                    "UPDATE inspection_equipment SET state='valid' WHERE equipment_id=?",
                    (incident["equipment_id"],),
                )
            self._audit("equipment", incident["equipment_id"], "calibration.incident_resolved", actor_id,
                        {"incident_id": incident_id, "release": release, "resolution": resolution})
        return self.incident(incident_id)

    # ------------------------------------------------------------- 管理查询

    def material_destination_report(self, actor_id: str, device_id: str | None = None) -> dict[str, Any]:
        """说明材料最终去向：复用进哪台新产品、报废到何处、拆解残余如何处置，
        以及尚未终结的部件。"""
        self._require(actor_id, "report.read")
        if device_id is not None:
            self.device(device_id)
        sql = (
            "SELECT c.component_id,c.component_type,c.name,c.weight_kg,c.state AS component_state,"
            "c.device_id,d.kind,d.destination,d.product_id,d.policy_id,d.policy_version,d.policy_sha256,"
            "d.certification_id FROM components c LEFT JOIN dispositions d ON d.component_id=c.component_id "
            + ("WHERE c.device_id=? " if device_id else "")
            + "ORDER BY c.device_id,c.sequence"
        )
        rows = self.connection.execute(sql, (device_id,) if device_id else ()).fetchall()
        totals: dict[str, Decimal] = {"new_product": Decimal("0"), "scrap": Decimal("0"),
                                      "residue": Decimal("0"), "undisposed": Decimal("0")}
        items: list[dict[str, Any]] = [dict(row) for row in rows]
        for row in rows:
            if row["kind"] == "new_product":
                totals["new_product"] += Decimal(row["weight_kg"])
            elif row["kind"] == "scrap":
                totals["scrap"] += Decimal(row["weight_kg"])
            else:
                totals["undisposed"] += Decimal(row["weight_kg"])
        # 拆解残余（保温纸、油污等）作为独立去向行补入
        residue_sql = (
            "SELECT ds.device_id,ds.residue_weight_kg,ds.residue_destination "
            "FROM disassemblies ds "
            + ("WHERE ds.device_id=? " if device_id else "")
            + "ORDER BY ds.device_id"
        )
        for residue in self.connection.execute(residue_sql, (device_id,) if device_id else ()).fetchall():
            weight = Decimal(residue["residue_weight_kg"])
            if weight > 0:
                totals["residue"] += weight
                items.append({
                    "component_id": None,
                    "component_type": "disassembly_residue",
                    "name": "拆解残余",
                    "weight_kg": residue["residue_weight_kg"],
                    "component_state": "disposed",
                    "device_id": residue["device_id"],
                    "kind": "residue",
                    "destination": residue["residue_destination"],
                    "product_id": None,
                    "policy_id": None,
                    "policy_version": None,
                    "policy_sha256": None,
                    "certification_id": None,
                })
        return {
            "device_id": device_id,
            "components": items,
            "weight_summary_kg": {key: format(value, "f") for key, value in totals.items()},
        }

    def reuse_decision_evidence(self, actor_id: str, component_id: str) -> dict[str, Any]:
        """说明每次复用决定引用的证据版本：规则版本、认证证据摘要、检测/修复依据。"""
        self._require(actor_id, "report.read")
        component = self._component(component_id)
        disposition = self._disposition_of(component_id)
        if disposition is None:
            raise InvalidState(f"部件 {component_id} 尚未作出复用决定")
        evidence: dict[str, Any] = {
            "component_id": component_id,
            "component_type": component["component_type"],
            "disposition": disposition,
            "policy": self.reuse_policy(disposition["policy_id"], disposition["policy_version"]),
        }
        if disposition["certification_id"]:
            cert = self.connection.execute(
                "SELECT certification_id,batch_no,product_type,evidence_sha256,state FROM certifications "
                "WHERE certification_id=?",
                (disposition["certification_id"],),
            ).fetchone()
            evidence["certification"] = dict(cert)
            evidence["certification_evidence"] = next(
                item for item in json.loads(self.connection.execute(
                    "SELECT evidence_json FROM certifications WHERE certification_id=?",
                    (disposition["certification_id"],),
                ).fetchone()["evidence_json"])["components"]
                if item["component_id"] == component_id
            )
        return evidence
