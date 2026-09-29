"""再制造追踪服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS reman_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN
        ('intake','dismantler','inspector','engineer','certifier','logistics','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 回收设备（旧变压器等）：采购部门可见回收重量
CREATE TABLE IF NOT EXISTS recovered_devices (
    device_id TEXT PRIMARY KEY,
    device_type TEXT NOT NULL,
    model TEXT NOT NULL,
    serial_no TEXT NOT NULL,
    source TEXT NOT NULL,
    recovered_weight_kg TEXT NOT NULL,
    recovered_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'registered'
        CHECK(state IN ('registered','dismantled','closed')),
    registered_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(device_type, serial_no)
);

-- 一次拆解事件，建立设备 -> 部件的谱系父边
CREATE TABLE IF NOT EXISTS disassemblies (
    disassembly_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL UNIQUE REFERENCES recovered_devices(device_id),
    dismantled_at TEXT NOT NULL,
    residue_weight_kg TEXT NOT NULL DEFAULT '0',
    residue_destination TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    dismantled_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES recovered_devices(device_id),
    disassembly_id TEXT NOT NULL REFERENCES disassemblies(disassembly_id),
    component_type TEXT NOT NULL,
    name TEXT NOT NULL,
    material TEXT NOT NULL DEFAULT '',
    weight_kg TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'recovered'
        CHECK(state IN ('recovered','inspected','repaired','qualified','reused','scrapped')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_components_device ON components(device_id, sequence);
CREATE INDEX IF NOT EXISTS idx_components_type ON components(component_type);

-- 检测设备与校准历史
CREATE TABLE IF NOT EXISTS inspection_equipment (
    equipment_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    serial_no TEXT NOT NULL,
    calibration_valid_from TEXT NOT NULL,
    calibration_valid_to TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'valid'
        CHECK(state IN ('valid','incident','retired')),
    registered_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES inspection_equipment(equipment_id),
    calibrated_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    certificate_ref TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calibration_equipment
ON calibration_events(equipment_id, event_id);

-- 检测记录：方法、设备、判定准则与实测值不可变；校准失效时仅打嫌疑标记
CREATE TABLE IF NOT EXISTS inspection_records (
    inspection_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    method_code TEXT NOT NULL,
    method_name TEXT NOT NULL,
    equipment_id TEXT NOT NULL REFERENCES inspection_equipment(equipment_id),
    inspected_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pass','fail')),
    measured_json TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'valid' CHECK(state IN ('valid','suspect')),
    inspector TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inspection_component
ON inspection_records(component_id, inspected_at);
CREATE INDEX IF NOT EXISTS idx_inspection_equipment
ON inspection_records(equipment_id, inspected_at, state);

-- 修复工艺规范：只追加新版本，永不原地修改
CREATE TABLE IF NOT EXISTS process_specs (
    spec_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    created_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(spec_id, version)
);

-- 复用规则：部件类型 -> 允许进入的新产品类型 / 是否允许报废
CREATE TABLE IF NOT EXISTS reuse_policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    created_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(policy_id, version)
);

-- 部件修复记录：签发时钉住所引用的工艺规范版本与摘要
CREATE TABLE IF NOT EXISTS repairs (
    repair_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    spec_id TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    spec_sha256 TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    repaired_at TEXT NOT NULL,
    repaired_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_repairs_component ON repairs(component_id, repaired_at);

-- 再认证批次（决定 + 证据快照，证据一经签发不可变）
CREATE TABLE IF NOT EXISTS certifications (
    certification_id TEXT PRIMARY KEY,
    batch_no TEXT NOT NULL UNIQUE,
    product_type TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'issued'
        CHECK(decision IN ('issued','rejected')),
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK(state IN ('issued','suspended','released','rejected','revoked')),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    policy_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES reman_users(user_id),
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certification_components (
    certification_id TEXT NOT NULL REFERENCES certifications(certification_id),
    component_id TEXT NOT NULL UNIQUE REFERENCES components(component_id),
    PRIMARY KEY(certification_id, component_id)
);

CREATE TABLE IF NOT EXISTS certification_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    certification_id TEXT NOT NULL REFERENCES certifications(certification_id),
    action TEXT NOT NULL CHECK(action IN
        ('issued','suspended','released','release_deferred','revoked')),
    reason TEXT NOT NULL DEFAULT '',
    incident_id TEXT,
    actor_id TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cert_events ON certification_events(certification_id, event_id);

-- 用再认证部件装成的新产品；交付状态独立于认证签发状态
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    product_type TEXT NOT NULL,
    certification_id TEXT NOT NULL REFERENCES certifications(certification_id),
    state TEXT NOT NULL DEFAULT 'built' CHECK(state IN ('built','suspended','delivered')),
    built_at TEXT NOT NULL,
    delivered_at TEXT,
    customer TEXT NOT NULL DEFAULT '',
    built_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_cert ON products(certification_id, state);

-- 部件最终去向（复用决定）：钉住规则证据版本，每个部件只能有一条终态去向
CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL UNIQUE REFERENCES components(component_id),
    kind TEXT NOT NULL CHECK(kind IN ('new_product','scrap')),
    product_id TEXT REFERENCES products(product_id),
    destination TEXT NOT NULL,
    weight_kg TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    policy_sha256 TEXT NOT NULL,
    certification_id TEXT REFERENCES certifications(certification_id),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL REFERENCES reman_users(user_id),
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 检测设备校准失效事件与受影响认证批次
CREATE TABLE IF NOT EXISTS calibration_incidents (
    incident_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES inspection_equipment(equipment_id),
    invalid_from TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
    affected_inspection_count INTEGER NOT NULL DEFAULT 0,
    affected_certification_count INTEGER NOT NULL DEFAULT 0,
    suspended_product_count INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    created_by TEXT NOT NULL REFERENCES reman_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_impacts (
    incident_id TEXT NOT NULL REFERENCES calibration_incidents(incident_id),
    certification_id TEXT NOT NULL REFERENCES certifications(certification_id),
    suspended_product_count INTEGER NOT NULL DEFAULT 0,
    delivered_product_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(incident_id, certification_id)
);

CREATE TABLE IF NOT EXISTS reman_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS reman_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reman_audit_entity
ON reman_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
