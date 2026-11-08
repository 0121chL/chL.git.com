"""工单流转与遥测时间过滤（数据一致性回归）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_telemetry_time_window_excludes_old_rows(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)

    now = datetime.now(timezone.utc)
    db.insert_telemetry(path, _iso(now), "dev-1", "ch4", 0.5, "%")
    db.insert_telemetry(path, _iso(now - timedelta(minutes=10)), "dev-1", "ch4", 0.6, "%")
    db.insert_telemetry(path, _iso(now - timedelta(hours=6)), "dev-1", "ch4", 0.9, "%")

    recent = db.list_telemetry(path, device_id="dev-1", minutes=30)
    assert len(recent) == 2, f"30 分钟窗口不应包含 6 小时前的数据：{recent}"


def test_telemetry_ts_normalized_to_utc(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    db.insert_telemetry(path, "2026-11-08T12:00:00+08:00", "dev-1", "ch4", 0.5, "%")
    with db.connect(path) as conn:
        ts = conn.execute("SELECT ts FROM telemetry").fetchone()[0]
    assert ts.endswith("Z")
    assert ts.startswith("2026-11-08T04:00:00")


def test_work_order_lifecycle(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)

    order = db.insert_work_order(path, "evt-1", '{"rule_id": "R-CH4-HIGH"}')
    assert order["status"] == "created"

    assigned = db.update_work_order(path, order["id"], status="assigned", assignee="张三", assigned_at=db.utcnow())
    assert assigned["status"] == "assigned"
    assert assigned["assignee"] == "张三"

    closed = db.update_work_order(
        path, order["id"], status="closed", closed_at=db.utcnow(), resolution="已复测合格"
    )
    assert closed["status"] == "closed"
    assert closed["resolution"] == "已复测合格"

    reopened = db.update_work_order(path, order["id"], status="created", closed_at=None, resolution=None)
    assert reopened["status"] == "created"
    assert reopened["resolution"] is None

    listed = db.list_work_orders(path, limit=5)
    assert listed[0]["assignee"] == "张三"


def test_update_work_order_ignores_unknown_fields(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    order = db.insert_work_order(path, "evt-2", "{}")
    db.update_work_order(path, order["id"], status="assigned", content="hack", id=999)
    row = db.get_work_order_by_id(path, order["id"])
    assert row["status"] == "assigned"
