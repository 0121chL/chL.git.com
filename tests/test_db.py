"""SQLite 层：工单/审批幂等、遥测落库与清理。"""

from __future__ import annotations

from datetime import datetime, timedelta

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


# --------------------------------------------------------------------------- #
# 负责人表 / 值班轮值
# --------------------------------------------------------------------------- #
def test_pick_on_duty_prefers_least_loaded(tmp_path):
    path = tmp_path / "duty.db"
    db.init_db(path)
    first = db.create_staff(path, "甲", team="T", on_duty=1)
    second = db.create_staff(path, "乙", team="T", on_duty=1)
    db.create_staff(path, "丙", team="T", on_duty=0)  # 不当班，不该被选中

    picked = db.pick_on_duty_staff(path)
    assert picked["name"] == "甲"

    # 甲身上有一张在办工单后，应轮到乙
    row = db.insert_work_order(path, "evt-duty-1", "{}")
    db.update_work_order(path, row["id"], assignee="甲", status="assigned")
    assert db.pick_on_duty_staff(path)["name"] == "乙"

    # 全部取消当班 → 没有可选的人
    db.update_staff(path, first["id"], on_duty=0)
    db.update_staff(path, second["id"], on_duty=0)
    assert db.pick_on_duty_staff(path) is None


def test_pick_on_duty_respects_team(tmp_path):
    path = tmp_path / "duty2.db"
    db.init_db(path)
    db.create_staff(path, "甲", team="T1", on_duty=1)
    db.create_staff(path, "乙", team="T2", on_duty=1)
    assert db.pick_on_duty_staff(path, team="T2")["name"] == "乙"
    # 指定班组里没人当班时，退化为全体当班（不至于派不出去）
    assert db.pick_on_duty_staff(path, team="T9")["name"] == "甲"


def test_closed_work_orders_do_not_count_as_load(tmp_path):
    path = tmp_path / "duty3.db"
    db.init_db(path)
    a = db.create_staff(path, "甲", team="T", on_duty=1)
    db.create_staff(path, "乙", team="T", on_duty=1)
    row = db.insert_work_order(path, "evt-duty-2", "{}")
    db.update_work_order(path, row["id"], assignee="甲", status="closed")
    # 关闭的工单不算在办，所以还是选甲（id 更小）
    assert db.pick_on_duty_staff(path)["name"] == "甲"
    assert a["on_duty"] == 1


# --------------------------------------------------------------------------- #
# 排班表
# --------------------------------------------------------------------------- #
def test_shift_is_active_handles_overnight():
    shift = {"start_time": "20:00", "end_time": "08:00", "weekdays": "1,2,3,4,5,6,7"}
    assert db.shift_is_active(shift, datetime(2026, 11, 16, 22, 0)) is True   # 周一 22:00
    assert db.shift_is_active(shift, datetime(2026, 11, 17, 3, 0)) is True    # 周二 03:00（跨夜那半段）
    assert db.shift_is_active(shift, datetime(2026, 11, 17, 12, 0)) is False  # 周二中午


def test_shift_is_active_respects_weekdays():
    workday = {"start_time": "08:00", "end_time": "16:00", "weekdays": "1,2,3,4,5"}
    assert db.shift_is_active(workday, datetime(2026, 11, 16, 10, 0)) is True   # 周一
    assert db.shift_is_active(workday, datetime(2026, 11, 15, 10, 0)) is False  # 周日


def test_sync_duty_only_touches_scheduled_staff(tmp_path):
    path = tmp_path / "shift.db"
    db.init_db(path)
    today = datetime.now().isoweekday()
    other_day = today % 7 + 1

    now = datetime.now()
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")

    on_now = db.create_staff(path, "甲", team="T")
    off_now = db.create_staff(path, "乙", team="T")
    manual = db.create_staff(path, "丙", team="T", on_duty=1)

    db.create_shift(path, "当前班", on_now["id"], start, end, team="T", weekdays="1,2,3,4,5,6,7")
    db.create_shift(path, "别的日子", off_now["id"], start, end, team="T", weekdays=str(other_day))

    info = db.sync_duty(path, now)
    assert info["configured"] is True
    rows = {row["name"]: row for row in db.list_staff(path)}
    assert rows["甲"]["on_duty"] == 1
    assert rows["乙"]["on_duty"] == 0
    # 没排班的人保留手工设置
    assert rows["丙"]["on_duty"] == 1
    assert manual["on_duty"] == 1
