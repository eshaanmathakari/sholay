"""Oracle for Flow #3 (Proton Mail, no public API).

There is no API to verify mailbox state against, so the verifier is a human: after the
agent marks the top-N inbox emails read, `run_oracle` pops the legacy Gate-C review
dialog (`review.request_final_review`) and the human's click — Approve / No / Feedback —
becomes the run's status. The emitted list of marked-read emails (+ unread before/after)
only populates the dialog summary so the reviewer sees what they're approving.

This mirrors the measure/demo split (docs/PLAN.md D6/D7): Flow #1 is machine-verified
against an independent quote; a no-API flow is human-verified by design — which also
revives the human-in-the-loop safety story the autonomous flows turn off. `run_oracle`
takes an injectable `reviewer` so tests never pop a real dialog.
"""
from dataclasses import dataclass
from typing import Optional

import review

# Gate-C decision → run status. "needs_review" (reviewer wants another look / left
# feedback) maps to 'error' so it scores as neither a clean pass nor a fail.
_DECISION_TO_STATUS = {"pass": "pass", "fail": "fail", "needs_review": "error"}


@dataclass
class OracleResult:
    status: str                      # "pass" | "fail" | "error"
    fact_match: Optional[bool]       # always None — no machine-checkable fact without an API
    reasons: list                    # human-readable explanation of the verdict


def _marked(emit: dict) -> list:
    """Normalize the emitted marked-read list into [(sender, subject), ...]."""
    out = []
    for it in emit.get("marked_read") or []:
        if isinstance(it, dict):
            out.append((str(it.get("sender", "?")).strip(), str(it.get("subject", "?")).strip()))
        elif isinstance(it, str) and it.strip():
            out.append(("?", it.strip()))
    return out


def summarize(emit: dict) -> str:
    """Human-facing summary of what the agent did, shown in the approval dialog."""
    items = _marked(emit)
    head = f"Agent marked {len(items)} email(s) as read"
    ub, ua = emit.get("unread_before"), emit.get("unread_after")
    if isinstance(ub, int) and isinstance(ua, int):
        head += f" (unread {ub} → {ua})"
    lines = [f"{i + 1}. {snd} — {subj}" for i, (snd, subj) in enumerate(items)]
    return head + (":\n" + "\n".join(lines) if lines else "")


def _default_reviewer(summary: str) -> str:
    """Pop the Gate-C approval dialog (Approve / Feedback / No) and return its decision."""
    policy = review.ReviewPolicy(mode="off", require_final_review=True, vscode_hidden=True)
    return review.request_final_review(policy, summary).get("decision") or "needs_review"


def run_oracle(emit: dict, config: dict, *, reviewer=None) -> OracleResult:
    """Human-gated scoring: summarize the agent's mark-read actions, ask the human to
    approve, and turn that verdict into the run status. There is no machine fact to check
    (no API), so `fact_match` is always None.
    """
    summary = summarize(emit)
    decision = (reviewer or _default_reviewer)(summary)
    status = _DECISION_TO_STATUS.get(decision, "error")

    reasons = [f"human final review: {decision}"]
    expected = config.get("count", 5)
    n = len(_marked(emit))
    if n != expected:
        reasons.append(f"note: agent reported {n} marked-read, playbook target is {expected}")
    reasons.append(summary)
    return OracleResult(status, None, reasons)
