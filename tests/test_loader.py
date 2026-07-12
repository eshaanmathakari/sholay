"""T1 — spec loader: valid spec parses; malformed specs fail fast (no silent run)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flows import loader
from flows.loader import SpecError


def _valid():
    return {
        "name": "demo", "app_type": "browser", "model": "claude-sonnet-4-6",
        "mode": "measure", "oracle": "tradingview",
        "steps": [{"id": "open", "goal": "Open the browser."},
                  {"id": "go", "goal": "Go to the site."}],
    }


def test_valid_spec_parses():
    spec = loader.validate(_valid())
    assert spec.name == "demo"
    assert spec.app_type == "browser"
    assert len(spec.steps) == 2
    assert spec.steps[0].id == "open"
    assert spec.steps_expected == 2          # defaults to len(steps)


def test_missing_steps_raises():
    d = _valid(); del d["steps"]
    _expect(SpecError, d)


def test_empty_steps_raises():
    d = _valid(); d["steps"] = []
    _expect(SpecError, d)


def test_bad_app_type_raises():
    d = _valid(); d["app_type"] = "mobile"
    _expect(SpecError, d)


def test_bad_mode_raises():
    d = _valid(); d["mode"] = "yolo"
    _expect(SpecError, d)


def test_step_missing_goal_raises():
    d = _valid(); d["steps"] = [{"id": "open"}]
    _expect(SpecError, d)


def test_missing_name_raises():
    d = _valid(); del d["name"]
    _expect(SpecError, d)


def test_multi_app_type_is_accepted():
    d = _valid(); d["app_type"] = "multi_app"
    assert loader.validate(d).app_type == "multi_app"


def test_real_github_pr_yaml_loads():
    """Validates the shipped Flow #2 spec — skipped if PyYAML isn't installed."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("SKIP test_real_github_pr_yaml_loads (PyYAML not installed)")
        return
    spec = loader.load(ROOT / "flows" / "github_pr.yaml")
    assert spec.name == "github_notion_intake"
    assert spec.app_type == "multi_app"
    assert spec.oracle == "github_pr"
    assert spec.model == "claude-sonnet-4-6"
    assert spec.steps_expected == 8
    assert spec.oracle_config["repo"] == "eshaanmathakari/sholay"
    assert spec.emit_schema == ["prs", "invoices",
                                "notion_pr_rows_added", "notion_invoice_rows_added"]


def test_real_tradingview_yaml_loads():
    """Validates the shipped spec — skipped if PyYAML isn't installed."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("SKIP test_real_tradingview_yaml_loads (PyYAML not installed)")
        return
    spec = loader.load(ROOT / "flows" / "tradingview.yaml")
    assert spec.name == "tradingview_spx"
    assert spec.app_type == "browser"
    assert spec.oracle == "tradingview"
    assert spec.steps_expected == 8
    assert spec.emit_schema == ["last_close", "tf_daily", "tf_4h", "patterns", "paragraph"]


def _expect(exc, data):
    try:
        loader.validate(data)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


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
