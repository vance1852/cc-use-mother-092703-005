"""再制造追踪与再认证服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path


# 每个连接一把可重入锁，串行化跨 HTTP 线程的事务。连接通常长生命周期、数量很少。
_LOCKS: dict[int, threading.RLock] = {}


def _lock_for(connection: sqlite3.Connection) -> threading.RLock:
    key = id(connection)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = threading.RLock()
        _LOCKS[key] = lock
    return lock


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 人员与角色 -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN
        ('intake','disassembly','inspector','process_engineer','quality','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 复用规则（版本化、不可变）---------------------------------------------
-- 同一种部件类型在同一规则版本中，可进入的新产品型号和报废处置互斥列举，
-- 部件去向必须命中其中之一，即"只能进入规则允许的新产品或报废流程"。

CREATE TABLE IF NOT EXISTS reuse_rule_versions (
    rule_set_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','superseded')),
    published_by TEXT NOT NULL REFERENCES users(user_id),
    published_at TEXT NOT NULL,
    PRIMARY KEY (rule_set_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS reuse_rule_items (
    rule_set_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    component_type TEXT NOT NULL,
    allowed_product_models TEXT NOT NULL,
    scrap_dispositions TEXT NOT NULL,
    PRIMARY KEY (rule_set_id, version, component_type),
    FOREIGN KEY (rule_set_id, version) REFERENCES reuse_rule_versions(rule_set_id, version)
);

-- 检测设备与校准 ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibrations (
    calibration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'valid' CHECK (state IN ('valid','expired','revoked')),
    certificate TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    CHECK (valid_from < valid_until)
);

CREATE INDEX IF NOT EXISTS idx_calibrations_instrument
ON calibrations(instrument_id, valid_from, valid_until);

-- 回收设备与拆解谱系 ------------------------------------------------------

CREATE TABLE IF NOT EXISTS recovered_devices (
    device_id TEXT PRIMARY KEY,
    device_type TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    source TEXT NOT NULL,
    recovered_weight_kg TEXT NOT NULL,
    received_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'received'
        CHECK (state IN ('received','disassembled','scrapped')),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES recovered_devices(device_id),
    component_type TEXT NOT NULL,
    material TEXT,
    weight_kg TEXT,
    disassembled_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'recovered'
        CHECK (state IN ('recovered','inspected','repaired','reused','scrapped')),
    disassembled_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_components_device ON components(device_id);
CREATE INDEX IF NOT EXISTS idx_components_type ON components(component_type);

-- 检测记录（检测方法 + 校准证据版本）--------------------------------------

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    method TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    calibration_id INTEGER NOT NULL REFERENCES calibrations(calibration_id),
    measured_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('pass','fail')),
    data_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    inspected_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inspections_component ON inspections(component_id);

-- 修复工艺（版本化、不可变）----------------------------------------------

CREATE TABLE IF NOT EXISTS process_spec_versions (
    process_code TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','superseded')),
    published_by TEXT NOT NULL REFERENCES users(user_id),
    published_at TEXT NOT NULL,
    PRIMARY KEY (process_code, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS repairs (
    repair_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    process_code TEXT NOT NULL,
    process_version INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    repaired_at TEXT NOT NULL,
    repaired_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (component_id, process_code, process_version, evidence_sha256),
    FOREIGN KEY (process_code, process_version)
        REFERENCES process_spec_versions(process_code, version)
);

-- 再制造产品与认证 --------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
    serial_number TEXT PRIMARY KEY,
    product_model TEXT NOT NULL,
    assembled_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'assembled'
        CHECK (state IN ('assembled','certified','rejected','delivered')),
    assembled_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_components (
    serial_number TEXT NOT NULL REFERENCES products(serial_number),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    rule_set_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    PRIMARY KEY (component_id),
    FOREIGN KEY (rule_set_id, rule_version)
        REFERENCES reuse_rule_versions(rule_set_id, version)
);

CREATE INDEX IF NOT EXISTS idx_product_components_product ON product_components(serial_number);

CREATE TABLE IF NOT EXISTS certifications (
    certificate_number TEXT PRIMARY KEY,
    serial_number TEXT NOT NULL REFERENCES products(serial_number),
    rule_set_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    decision TEXT NOT NULL CHECK (decision IN ('certified','rejected')),
    basis_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued' CHECK (state IN ('issued','revoked')),
    issued_by TEXT NOT NULL REFERENCES users(user_id),
    issued_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    FOREIGN KEY (rule_set_id, rule_version)
        REFERENCES reuse_rule_versions(rule_set_id, version)
);

CREATE INDEX IF NOT EXISTS idx_certifications_product ON certifications(serial_number);

-- 去向（每个部件最终唯一去向）--------------------------------------------

CREATE TABLE IF NOT EXISTS dispositions (
    component_id TEXT PRIMARY KEY REFERENCES components(component_id),
    kind TEXT NOT NULL CHECK (kind IN ('reuse','scrap')),
    rule_set_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    serial_number TEXT REFERENCES products(serial_number),
    scrap_disposition TEXT,
    destination TEXT,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    disposed_at TEXT NOT NULL,
    disposed_by TEXT NOT NULL REFERENCES users(user_id),
    CHECK (kind = 'scrap' OR serial_number IS NOT NULL),
    CHECK (kind = 'reuse' OR scrap_disposition IS NOT NULL),
    FOREIGN KEY (rule_set_id, rule_version)
        REFERENCES reuse_rule_versions(rule_set_id, version)
);

-- 校准失效影响与暂停 ------------------------------------------------------

CREATE TABLE IF NOT EXISTS calibration_incidents (
    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
    calibration_id INTEGER NOT NULL REFERENCES calibrations(calibration_id),
    reason TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    reported_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certification_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES calibration_incidents(incident_id),
    certificate_number TEXT NOT NULL REFERENCES certifications(certificate_number),
    state TEXT NOT NULL CHECK (state IN ('held','released','revoked')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    note TEXT,
    UNIQUE (incident_id, certificate_number)
);

CREATE TABLE IF NOT EXISTS product_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial_number TEXT NOT NULL REFERENCES products(serial_number),
    incident_id INTEGER NOT NULL REFERENCES calibration_incidents(incident_id),
    state TEXT NOT NULL DEFAULT 'held' CHECK (state IN ('held','released')),
    reason TEXT NOT NULL,
    held_at TEXT NOT NULL,
    released_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_product
ON product_holds(serial_number) WHERE state = 'held';

-- 审计事件 ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "reuse_rule_versions", "reuse_rule_items",
    "instruments", "calibrations", "recovered_devices", "components",
    "inspections", "process_spec_versions", "repairs", "products",
    "product_components", "certifications", "dispositions",
    "calibration_incidents", "certification_holds", "product_holds",
    "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务为多线程模型，允许连接跨线程使用；所有写事务由连接级锁串行化，
    请求级再在 API 层整体加锁，避免同一连接被并发执行。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    initialize(connection)
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。连接级锁保证多线程下事务不被交错。"""

    with _lock_for(connection):
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化表结构，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
