"""T8 — github_pr oracle: machine-checks every emitted PR against the GitHub API,
invoices counted but human-gated, injected fetchers/reviewer, GitHub failure → 'error'."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracles.github_pr import (
    OracleResult, _coerce_number, evaluate, fetch_github_pr, run_oracle,
)

# Two open PRs on the repo (ground truth), and an emit that read them correctly.
GH = {
    12: {"number": 12, "title": "Add site-wide navigation", "author": "eshaanmathakari"},
    13: {"number": 13, "title": "Swap default model to Sonnet 5", "author": "eshaanmathakari"},
}
EMIT = {
    "prs": [
        {"number": 12, "title": "Add site-wide navigation", "author": "eshaanmathakari"},
        {"number": 13, "title": "Swap default model to Sonnet 5", "author": "eshaanmathakari"},
    ],
    "invoices": [
        {"sender": "billing@anthropic.com", "subject": "Your July invoice"},
        {"sender": "receipts@notion.so", "subject": "Payment receipt"},
    ],
    "notion_pr_rows_added": 2,
    "notion_invoice_rows_added": 2,
}
CFG = {"repo": "eshaanmathakari/sholay", "status_value": "Pending"}


def _fetcher(repo, n):
    return GH[n]


def test_coerce_number_handles_int_str_and_hash():
    assert _coerce_number(12) == 12
    assert _coerce_number("12") == 12
    assert _coerce_number("#12") == 12
    assert _coerce_number("twelve") is None


def test_evaluate_all_prs_match_passes():
    res = evaluate(EMIT, CFG, GH)
    assert res.status == "pass" and res.fact_match is True


def test_evaluate_no_prs_fails():
    res = evaluate({"prs": [], "invoices": EMIT["invoices"]}, CFG, {})
    assert res.status == "fail" and res.fact_match is False


def test_evaluate_one_pr_title_mismatch_fails():
    emit = {"prs": [dict(EMIT["prs"][0], title="Wrong title"), EMIT["prs"][1]],
            "invoices": EMIT["invoices"]}
    res = evaluate(emit, CFG, GH)
    assert res.status == "fail" and res.fact_match is False


def test_evaluate_unknown_pr_number_fails():
    emit = {"prs": [{"number": 999, "title": "x", "author": "y"}], "invoices": []}
    res = evaluate(emit, CFG, GH)
    assert res.status == "fail" and res.fact_match is False


def test_evaluate_case_and_whitespace_forgiven():
    emit = {"prs": [{"number": 12, "title": "  ADD site-wide Navigation ",
                     "author": "EshaanMathakari"}],
            "invoices": [{"sender": "a", "subject": "b"}]}
    res = evaluate(emit, CFG, GH)
    assert res.fact_match is True


def test_evaluate_missing_invoices_is_noted_not_a_machine_fail():
    emit = {"prs": EMIT["prs"], "invoices": []}
    res = evaluate(emit, CFG, GH)
    # PRs all match → machine passes; the empty inbox is left to the human gate.
    assert res.status == "pass" and res.fact_match is True
    assert any("no invoice emails" in r for r in res.reasons)


def test_run_oracle_pass_requires_human_approval():
    assert run_oracle(EMIT, CFG, reviewer=lambda s: "pass", pr_fetcher=_fetcher).status == "pass"
    assert run_oracle(EMIT, CFG, reviewer=lambda s: "fail", pr_fetcher=_fetcher).status == "fail"
    assert run_oracle(EMIT, CFG, reviewer=lambda s: "needs_review", pr_fetcher=_fetcher).status == "error"


def test_run_oracle_machine_mismatch_fails_regardless_of_reviewer():
    emit = {"prs": [dict(EMIT["prs"][0], author="someone-else")], "invoices": EMIT["invoices"]}
    res = run_oracle(emit, CFG, reviewer=lambda s: "pass", pr_fetcher=_fetcher)
    assert res.status == "fail" and res.fact_match is False


def test_run_oracle_github_failure_is_error_not_fail():
    def boom(repo, n):
        raise RuntimeError("github down")
    res = run_oracle(EMIT, CFG, reviewer=lambda s: "pass", pr_fetcher=boom)
    assert res.status == "error" and res.fact_match is None


def test_run_oracle_non_numeric_pr_number_fails():
    emit = {"prs": [{"number": "twelve", "title": "x", "author": "y"}], "invoices": []}
    res = run_oracle(emit, CFG, reviewer=lambda s: "pass", pr_fetcher=_fetcher)
    assert res.status == "fail"


def test_run_oracle_coerces_hash_prefixed_number():
    emit = {"prs": [{"number": "#12", "title": GH[12]["title"], "author": GH[12]["author"]}],
            "invoices": [{"sender": "a", "subject": "b"}]}
    res = run_oracle(emit, CFG, reviewer=lambda s: "pass", pr_fetcher=_fetcher)
    assert res.status == "pass"


def test_run_oracle_missing_repo_is_error():
    res = run_oracle(EMIT, {}, reviewer=lambda s: "pass", pr_fetcher=_fetcher)
    assert res.status == "error"


def test_summarize_dialog_mentions_both_legs():
    seen = {}
    run_oracle(EMIT, CFG, reviewer=lambda s: seen.setdefault("s", s) and "pass" or "pass",
               pr_fetcher=_fetcher)
    s = seen["s"]
    assert "GitHub API" in s and "COA test" in s and "invoice" in s.lower()


def test_fetch_github_pr_parses_api_shape():
    def fake_get(url, **kw):
        assert url == "https://api.github.com/repos/eshaanmathakari/sholay/pulls/12"
        return {"number": 12, "title": "Add nav", "user": {"login": "eshaanmathakari"}}
    pr = fetch_github_pr("eshaanmathakari/sholay", 12, _get=fake_get)
    assert pr == {"number": 12, "title": "Add nav", "author": "eshaanmathakari"}


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
