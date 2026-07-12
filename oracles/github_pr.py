"""Oracle for Flow #2 (GitHub PRs + Proton invoices → one Notion tracker).

Mixed verification, one leg per capability:

  * MACHINE — the GitHub REST API (public repo, no key needed) is independent ground
    truth for the PRs the agent read via vision. The oracle fetches each emitted PR by
    number and confirms its title + author match — that comparison is `fact_match`, the
    "did the agent read GitHub faithfully" verdict. This backbone stays machine-verified
    with no credentials of any kind.
  * HUMAN — the invoice→Notion leg can't be machine-checked: email has no API, and the
    agent is populating Notion by vision (no Notion API key in this setup). So a Gate-C
    approval dialog asks the reviewer to confirm the Notion rows (both Sources) exist and
    that the logged emails are genuinely invoice/billing mail. Same pattern as
    oracles/proton.py. (Injectable `reviewer` so tests never pop a dialog.)

`evaluate()` is pure (inject the already-fetched GitHub PRs) so it's unit-testable
without the network. `run_oracle()` adds the live GitHub fetch and turns a fetch failure
into status "error" (infra noise must not count as an accuracy fail).
"""
import json
from dataclasses import dataclass
from typing import Optional
from urllib.request import Request, urlopen

import review

GITHUB_PR_URL = "https://api.github.com/repos/{repo}/pulls/{number}"
USER_AGENT = "coa-test-oracle"

# Gate-C decision → run status (same mapping as oracles/proton.py).
_DECISION_TO_STATUS = {"pass": "pass", "fail": "fail", "needs_review": "error"}


@dataclass
class OracleResult:
    status: str                      # "pass" | "fail" | "error"
    fact_match: Optional[bool]      # every emitted PR matched the GitHub API; None if not evaluated
    reasons: list                    # human-readable explanation of the verdict


def _http_json(url: str, *, timeout: int = 10) -> dict:
    """GET a JSON API and return the parsed body. Injectable in tests via _get."""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_github_pr(repo: str, number: int, *, _get=None) -> dict:
    """Ground truth for one PR: {number, title, author} from the GitHub REST API."""
    get = _get or _http_json
    data = get(GITHUB_PR_URL.format(repo=repo, number=number))
    return {
        "number": data["number"],
        "title": data["title"],
        "author": (data.get("user") or {}).get("login", ""),
    }


def _coerce_number(raw) -> Optional[int]:
    """Turn an emitted PR number (int, '12', or '#12') into an int, or None."""
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).lstrip("#").strip())
    except (TypeError, ValueError):
        return None


def evaluate(emit: dict, config: dict, gh_by_number: dict) -> OracleResult:
    """Pure machine scoring: every emitted PR must match its GitHub API record
    (title + author). Invoices are counted but verified by the human, not here."""
    reasons = []
    checks = []

    def check(ok: bool, reason: str):
        checks.append(ok)
        reasons.append(("✓ " if ok else "✗ ") + reason)

    prs = emit.get("prs") or []
    if not prs:
        check(False, "no PRs in emit — nothing to verify against GitHub")

    for pr in prs:
        num = _coerce_number(pr.get("number"))
        gt = gh_by_number.get(num)
        if gt is None:
            check(False, f"emitted PR #{pr.get('number')!r} is not an open PR on GitHub")
            continue
        check(
            str(pr.get("title") or "").strip().lower() == (gt["title"] or "").strip().lower(),
            f"PR #{num} title {pr.get('title')!r} vs GitHub {gt['title']!r}",
        )
        check(
            str(pr.get("author") or "").strip().lower() == (gt["author"] or "").strip().lower(),
            f"PR #{num} author {pr.get('author')!r} vs GitHub {gt['author']!r}",
        )

    invoices = emit.get("invoices") or []
    reasons.append(f"agent logged {len(prs)} PR row(s) and {len(invoices)} invoice row(s)")
    if not invoices:
        reasons.append("note: no invoice emails logged — human should confirm the inbox")

    fact_match = bool(checks) and all(checks)
    # Machine can only vouch for the PR reads; the run still needs the human gate to pass.
    return OracleResult("pass" if fact_match else "fail", fact_match, reasons)


def summarize(emit: dict, machine: OracleResult) -> str:
    """Human-facing summary shown in the approval dialog."""
    prs = emit.get("prs") or []
    invoices = emit.get("invoices") or []
    lines = [
        f"Machine check (agent's PR reads vs GitHub API): "
        f"{'MATCH' if machine.fact_match else 'MISMATCH'}",
        "",
        f"PR rows logged to Notion ({len(prs)}):",
    ]
    for pr in prs:
        lines.append(f"  • #{pr.get('number')} — {pr.get('title')!r} by {pr.get('author')}")
    lines.append(f"Invoice rows logged to Notion ({len(invoices)}):")
    for inv in invoices:
        lines.append(f"  • {inv.get('sender')} — {inv.get('subject')!r}")
    lines.append("")
    lines.append("Confirm the HUMAN legs: does the 'COA test' database show every row above "
                 "(Source set correctly), and are the invoice rows genuinely billing emails?")
    return "\n".join(lines)


def _default_reviewer(summary: str) -> str:
    """Pop the Gate-C approval dialog (Approve / Feedback / No) and return its decision."""
    policy = review.ReviewPolicy(mode="off", require_final_review=True, vscode_hidden=True)
    return review.request_final_review(policy, summary).get("decision") or "needs_review"


def run_oracle(emit: dict, config: dict, *, reviewer=None, pr_fetcher=fetch_github_pr) -> OracleResult:
    """Live scoring: GitHub machine checks on the PR reads, then the human Notion/invoice gate.

    pass  = every emitted PR matches the GitHub API AND the human approved the Notion rows.
    fail  = a PR read doesn't match GitHub (or none were logged).
    error = a GitHub fetch failed (couldn't verify — never an accuracy fail), or the
            reviewer asked for another look.
    """
    repo = config.get("repo")
    if not repo:
        return OracleResult("error", None, ["oracle_config.repo missing from the flow spec"])

    prs = emit.get("prs") or []
    gh_by_number = {}
    for pr in prs:
        num = _coerce_number(pr.get("number"))
        if num is None:
            return OracleResult("fail", None, [f"emitted PR number {pr.get('number')!r} is not a number"])
        try:
            gh_by_number[num] = pr_fetcher(repo, num)
        except Exception as e:
            return OracleResult("error", None, [f"GitHub fetch failed for #{num}: {e}"])

    machine = evaluate(emit, config, gh_by_number)

    decision = (reviewer or _default_reviewer)(summarize(emit, machine))
    gate_status = _DECISION_TO_STATUS.get(decision, "error")
    reasons = machine.reasons + [f"human Notion+invoice review: {decision}"]

    if machine.status != "pass":
        return OracleResult(machine.status, machine.fact_match, reasons)
    return OracleResult(gate_status, machine.fact_match, reasons)
