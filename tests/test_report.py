"""T7 — report: CLI and HTML show identical numbers; multi-flow renders; empty db is graceful."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metrics_db
import report


def _run(run_id, flow, app_type, status, cost, steps=10, tokens=1000):
    return {
        "run_id": run_id, "ts": f"2026-06-16T00:00:{int(run_id[-2:]):02d}", "flow": flow,
        "app_type": app_type, "model": "claude-sonnet-4-6", "mode": "measure",
        "status": status, "steps": steps, "in_tok": tokens, "out_tok": 0,
        "cache_read": 0, "cache_write": 0, "cost_usd": cost, "latency_s": 5.0,
        "fact_match": 1 if status == "pass" else 0,
    }


def _seed(conn):
    rows = [
        _run("r01", "tradingview_spx", "browser", "pass", 0.20),
        _run("r02", "tradingview_spx", "browser", "pass", 0.20),
        _run("r03", "tradingview_spx", "browser", "fail", 0.20),
        _run("r04", "proton_promos", "no_api", "pass", 0.10),
    ]
    for r in rows:
        metrics_db.insert_run(conn, r)


def test_cost_per_success_ge_avg_cost():
    assert report._cost_per_success(0.20, 66.7) >= 0.20
    assert report._cost_per_success(0.20, 100.0) == 0.20
    assert report._cost_per_success(0.20, 0) is None


def test_gather_has_both_flows_and_app_types():
    conn = metrics_db.connect(":memory:")
    _seed(conn)
    data = report.gather(conn)
    flows = {r["grp"] for r in data["by_flow"]}
    apps = {r["grp"] for r in data["by_app_type"]}
    assert flows == {"tradingview_spx", "proton_promos"}
    assert apps == {"browser", "no_api"}
    assert len(data["recent"]) == 4


def test_cli_and_html_agree_on_numbers():
    conn = metrics_db.connect(":memory:")
    _seed(conn)
    data = report.gather(conn, generated="2026-06-16T12:00:00")
    cli = report.render_cli(data)
    htm = report.render_html(data)
    # both flows present in both renderers
    for token in ("tradingview_spx", "proton_promos"):
        assert token in cli and token in htm
    # the browser flow's avg cost ($0.2000) appears identically in both
    assert "$0.2000" in cli and "$0.2000" in htm
    # the cross-app-type comparison the manager asked for is in the HTML
    assert "cross-type comparison" in htm
    # time-per-run is surfaced in both (every seeded run has latency_s=5.0)
    assert "time/run" in cli and "time/run" in htm
    assert "5.0s" in cli and "5.0s" in htm


def test_variability_section_renders():
    conn = metrics_db.connect(":memory:")
    _seed(conn)
    data = report.gather(conn, generated="2026-06-16T12:00:00")
    htm = report.render_html(data)
    cli = report.render_cli(data)
    assert "Run-to-run variability" in htm
    assert "<svg" in htm                      # the inline distribution chart
    assert "variability" in cli.lower()
    # gather() exposes a per-flow distribution payload
    flows = {r["grp"] for r in data["variability"]}
    assert flows == {"tradingview_spx", "proton_promos"}


def test_stats_skew_direction():
    """Right-skewed sample (one big outlier) -> positive skew; tight sample -> None."""
    right = report._stats([16, 16, 17, 23, 24, 32, 52])   # the real TradingView steps
    assert right["median"] == 23 and right["max"] == 52
    assert right["skew"] is not None and right["skew"] > 0
    assert report._stats([10])["skew"] is None             # n<3 -> not reported
    assert report._stats([])["n"] == 0                     # empty is safe


def test_svg_strip_is_wellformed_and_safe():
    svg = report._svg_strip(report._stats([16, 16, 52]))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "circle" in svg and "med" in svg
    assert report._svg_strip(report._stats([])).startswith("<svg")  # empty doesn't crash


def test_empty_db_is_graceful():
    conn = metrics_db.connect(":memory:")
    data = report.gather(conn)
    cli = report.render_cli(data)
    htm = report.render_html(data)
    assert "no runs" in cli.lower()
    assert "no runs" in htm.lower()


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
