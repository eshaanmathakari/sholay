"""T3 — metrics DB: round-trip, aggregates, unique run_id, feedback never mutates status."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metrics_db


def _run(run_id, status="pass", steps=10, cost=0.10, flow="tradingview_spx",
         app_type="browser", tokens=100):
    return {
        "run_id": run_id, "ts": "2026-06-16T00:00:00Z", "flow": flow,
        "app_type": app_type, "model": "claude-sonnet-4-6", "mode": "measure",
        "status": status, "steps": steps, "steps_expected": 8, "retries": 0,
        "misclicks": 0, "in_tok": tokens, "out_tok": 0, "cache_read": 0,
        "cache_write": 0, "cost_usd": cost, "latency_s": 1.0, "fact_match": 1,
        "run_dir": f"runs/{run_id}",
    }


def test_roundtrip():
    conn = metrics_db.connect(":memory:")
    row = _run("r1", cost=0.1234, steps=12)
    metrics_db.insert_run(conn, row)
    got = dict(conn.execute("SELECT * FROM runs WHERE run_id='r1'").fetchone())
    assert got["status"] == "pass"
    assert got["steps"] == 12
    assert abs(got["cost_usd"] - 0.1234) < 1e-9
    assert got["run_dir"] == "runs/r1"


def test_two_runs_two_rows():
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("a"))
    metrics_db.insert_run(conn, _run("b"))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_duplicate_run_id_raises():
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("dup"))
    try:
        metrics_db.insert_run(conn, _run("dup"))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("expected IntegrityError on duplicate run_id")


def test_aggregate():
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("a", status="pass", steps=10, cost=0.10))
    metrics_db.insert_run(conn, _run("b", status="pass", steps=20, cost=0.20))
    metrics_db.insert_run(conn, _run("c", status="fail", steps=30, cost=0.30))
    rows = metrics_db.aggregate(conn, group_by="flow")
    assert len(rows) == 1
    r = rows[0]
    assert r["runs"] == 3
    assert r["pass_pct"] == 66.7          # 2 of 3
    assert abs(r["avg_cost"] - 0.20) < 1e-9
    assert r["avg_steps"] == 20.0
    assert r["avg_latency"] == 1.0        # all three runs report latency_s=1.0


def test_aggregate_avg_latency():
    """Time-per-run is averaged across every status, like cost (not just scored runs)."""
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, dict(_run("a"), latency_s=100.0))
    metrics_db.insert_run(conn, dict(_run("b"), latency_s=300.0))
    r = metrics_db.aggregate(conn, group_by="flow")[0]
    assert r["avg_latency"] == 200.0


def test_series_per_flow_values():
    """series() returns each flow's raw per-run values in run order (for skew charts)."""
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("a", steps=10))
    metrics_db.insert_run(conn, _run("b", steps=20))
    metrics_db.insert_run(conn, _run("c", steps=30, flow="proton", app_type="no_api"))
    s = metrics_db.series(conn, metric="steps", group_by="flow")
    assert s["tradingview_spx"] == [10, 20]
    assert s["proton"] == [30]


def test_series_rejects_bad_metric():
    conn = metrics_db.connect(":memory:")
    try:
        metrics_db.series(conn, metric="status")  # not numeric / not whitelisted
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-whitelisted metric")


def test_aggregate_excludes_errors_from_pass_rate():
    """An 'error' run (couldn't verify) is infra noise, not an accuracy fail (D3):
    excluded from pass_pct's denominator, surfaced as `errors`, still counted in `runs`."""
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("a", status="pass"))
    metrics_db.insert_run(conn, _run("b", status="pass"))
    metrics_db.insert_run(conn, _run("c", status="fail"))
    metrics_db.insert_run(conn, _run("d", status="error"))
    r = metrics_db.aggregate(conn, group_by="flow")[0]
    assert r["runs"] == 4          # every status counted in the total
    assert r["scored"] == 3        # pass + fail
    assert r["errors"] == 1
    assert r["pass_pct"] == 66.7   # 2 pass / 3 scored — the error does NOT make it 50%


def test_aggregate_all_errors_pass_rate_is_null():
    """A group with only error runs has no scored runs → pass_pct is NULL, never a false 0%."""
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("e1", status="error"))
    metrics_db.insert_run(conn, _run("e2", status="error"))
    r = metrics_db.aggregate(conn, group_by="flow")[0]
    assert r["runs"] == 2
    assert r["scored"] == 0
    assert r["errors"] == 2
    assert r["pass_pct"] is None


def test_aggregate_rejects_bad_group():
    conn = metrics_db.connect(":memory:")
    try:
        metrics_db.aggregate(conn, group_by="status; DROP TABLE runs")
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-whitelisted group_by")


def test_feedback_does_not_mutate_status():
    conn = metrics_db.connect(":memory:")
    metrics_db.insert_run(conn, _run("r1", status="pass"))
    metrics_db.insert_feedback(conn, {
        "run_id": "r1", "ts": "2026-06-16T01:00:00Z",
        "reviewer": "eshaan", "verdict": "fail", "notes": "missed the 4H divergence",
    })
    status = conn.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()[0]
    assert status == "pass"  # oracle verdict unchanged
    fb = conn.execute("SELECT verdict FROM feedback WHERE run_id='r1'").fetchone()[0]
    assert fb == "fail"      # overriding verdict recorded separately


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
