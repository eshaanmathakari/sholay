"""T4 (Flow #3) — Proton oracle: the human Gate-C verdict becomes the run status.

The oracle has no machine-checkable fact (no API), so every test injects a fake `reviewer`
to stand in for the approval dialog — no dialog ever pops, and these run fully offline.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracles import proton

CONFIG = {"app_name": "Proton Mail", "count": 5}


def _emit(n=5, **over):
    e = {
        "marked_read": [{"sender": f"Sender {i}", "subject": f"Subject {i}"} for i in range(n)],
        "unread_before": 40,
        "unread_after": 40 - n,
    }
    e.update(over)
    return e


def _reviewer(decision):
    return lambda _summary: decision


def test_human_pass_is_pass():
    r = proton.run_oracle(_emit(), CONFIG, reviewer=_reviewer("pass"))
    assert r.status == "pass"
    assert r.fact_match is None        # no API → no machine fact, ever


def test_human_fail_is_fail():
    r = proton.run_oracle(_emit(), CONFIG, reviewer=_reviewer("fail"))
    assert r.status == "fail"


def test_needs_review_is_error():
    r = proton.run_oracle(_emit(), CONFIG, reviewer=_reviewer("needs_review"))
    assert r.status == "error"


def test_unknown_decision_is_error():
    r = proton.run_oracle(_emit(), CONFIG, reviewer=_reviewer("banana"))
    assert r.status == "error"


def test_verdict_is_human_even_if_count_wrong():
    # Agent only marked 3, but the human still approves → pass, with an explanatory note.
    r = proton.run_oracle(_emit(n=3), CONFIG, reviewer=_reviewer("pass"))
    assert r.status == "pass"
    assert any("reported 3" in s for s in r.reasons)


def test_summary_lists_emails_and_unread_delta():
    s = proton.summarize(_emit())
    assert "5 email(s) as read" in s
    assert "unread 40 → 35" in s
    assert "Sender 0" in s and "Subject 4" in s


def test_reviewer_receives_summary():
    seen = {}

    def rev(summary):
        seen["summary"] = summary
        return "pass"

    proton.run_oracle(_emit(), CONFIG, reviewer=rev)
    assert "marked 5" in seen["summary"]


def test_empty_marked_read_summary_is_safe():
    s = proton.summarize({"marked_read": []})
    assert "0 email(s)" in s


def test_string_marked_read_items_are_tolerated():
    r = proton.run_oracle({"marked_read": ["just a subject line"]}, CONFIG, reviewer=_reviewer("pass"))
    assert r.status == "pass"


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
