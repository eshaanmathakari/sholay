"""T6 — feedback: records a linked verdict + notes without mutating the run's status."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedback
import metrics_db


def _seed(conn, run_id="r1", status="pass"):
    metrics_db.insert_run(conn, {
        "run_id": run_id, "ts": "2026-06-16T00:00:00", "flow": "tradingview_spx",
        "app_type": "browser", "model": "claude-sonnet-4-6", "mode": "measure",
        "status": status,
    })


def test_record_writes_linked_row():
    conn = metrics_db.connect(":memory:")
    _seed(conn)
    feedback.record(conn, "r1", "fail", notes="flagged a phantom pattern", reviewer="eshaan")
    row = conn.execute("SELECT verdict, notes, reviewer FROM feedback WHERE run_id='r1'").fetchone()
    assert row["verdict"] == "fail"
    assert row["notes"] == "flagged a phantom pattern"
    assert row["reviewer"] == "eshaan"


def test_record_does_not_mutate_run_status():
    conn = metrics_db.connect(":memory:")
    _seed(conn, status="pass")
    feedback.record(conn, "r1", "fail", reviewer="eshaan")  # overriding verdict
    assert conn.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()[0] == "pass"


def test_invalid_verdict_raises():
    conn = metrics_db.connect(":memory:")
    _seed(conn)
    try:
        feedback.record(conn, "r1", "maybe")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an invalid verdict")


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS {t.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {t.__name__}"); traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
