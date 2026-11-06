#!/usr/bin/env python3
"""清空演示数据（事件 / 工单 / 审计），让计数与工单号从 1 开始。

    python scripts/reset_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def main() -> int:
    path = db.resolve_db_path(None)
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM work_orders")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('events','work_orders','audit_log')")
    print(f"[reset] 已清空 {path}")
    print("[reset] 若后端仍在运行，请重启 server.py 以清空内存中的待审批线程")
    return 0


if __name__ == "__main__":
    sys.exit(main())
