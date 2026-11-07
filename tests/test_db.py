"""SQLite 层：工单/审批幂等、遥测落库与清理。"""

from __future__ import annotations

import db


def test_work_order_idempotent(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    a = db.insert_work_order(path, "evt-1", '{"a":1}')
    b = db.insert_work_order(path, "evt-1", '{"a":2}')
    assert a["id"] == b["id"]
    assert db.count_work_orders(path, "evt-1") == 1


def test_approval_idempotent_keeps_first(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    first = db.record_approval(path, "evt-1", "approved", "张工", "同意")
    second = db.record_approval(path, "evt-1", "approved", "李工", "再来一次")
    assert first["approver"] == "张工"
    assert second["approver"] == "张工"  # 不覆盖
    assert db.get_approval(path, "evt-1")["comment"] == "同意"


def test_work_orders_join_approval(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    row = db.insert_work_order(path, "evt-1", "{}")
    db.record_approval(path, "evt-1", "approved", "张工", "ok", row["id"])
    orders = db.list_work_orders(path)
    assert orders[0]["approver"] == "张工"
    assert orders[0]["approval_comment"] == "ok"


def test_telemetry_insert_and_list(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    db.insert_telemetry(path, db.utcnow(), "dev-1", "ch4", 1.2, "%")
    db.insert_telemetry(path, db.utcnow(), "dev-1", "temp", 70.0, "C")
    points = db.list_telemetry(path, device_id="dev-1", minutes=10)
    assert len(points) == 2


def test_telemetry_purge(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    db.insert_telemetry(path, "2000-01-01T00:00:00+08:00", "dev-1", "ch4", 1.0, "%")
    removed = db.purge_telemetry(path, 24)
    assert removed == 1
    assert db.count_telemetry(path) == 0
