"""Human-in-the-loop review gates.

Three gates, enforced in code rather than only in the system prompt:

* Gate A (`request_plan_approval`): pre-run, before VSCode is hidden, on stdin.
* Gate B (`require_approval`): per-action, classified by `classify_risk`. Once
  VSCode is hidden the terminal is no longer visible to the user, so the gate
  prefers an AppleScript `display dialog`, then falls back to writing a
  pending-<step>.json/png pair and returning BLOCKED to the model.
* Gate C (`write_final_report` + `request_final_review`): post-run evidence
  review, always written even when blocked or failed.
"""
import json, shutil, subprocess, sys, time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


RISK_OBSERVE = "observe"
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_BLOCKED = "blocked"

REVIEW_MODES = ("off", "plan", "high_risk", "every_action")


_COMPUTER_RISK = {
    "screenshot": RISK_OBSERVE,
    "cursor_position": RISK_OBSERVE,
    "wait": RISK_OBSERVE,
    "mouse_move": RISK_LOW,
    "left_click": RISK_LOW,
    "right_click": RISK_LOW,
    "double_click": RISK_LOW,
    "triple_click": RISK_LOW,
    "middle_click": RISK_LOW,
    "scroll": RISK_LOW,
    "left_mouse_down": RISK_LOW,
    "left_mouse_up": RISK_LOW,
    "type": RISK_MEDIUM,
    "key": RISK_MEDIUM,
    "hold_key": RISK_MEDIUM,
    "left_click_drag": RISK_MEDIUM,
}

_MACOS_RISK = {
    "hide_vscode": RISK_LOW,
    "activate_app": RISK_LOW,
    "open_path": RISK_LOW,
    "verify_app_installed": RISK_OBSERVE,
    "eject_volume": RISK_MEDIUM,
    "install_app_from_dmg": RISK_HIGH,
    "run_applescript": RISK_HIGH,
    "safe_shell": RISK_HIGH,
}

_BLOCKED_KEY_COMBOS = {"cmd+q", "command+q"}


def classify_risk(tool_name: str, tool_input: dict, *, allow_typing_anything: bool = False) -> str:
    action = (tool_input or {}).get("action", "")
    if tool_name == "computer":
        risk = _COMPUTER_RISK.get(action, RISK_MEDIUM)
        if action == "key":
            combo = (tool_input.get("text") or "").lower().strip()
            if combo in _BLOCKED_KEY_COMBOS:
                return RISK_BLOCKED
        if action == "type" and not allow_typing_anything:
            text = tool_input.get("text") or ""
            # B3: demoted from BLOCKED to HIGH. The heuristic catches license keys,
            # API tokens, and "MyEmail123"-style strings as easily as real passwords;
            # let the human approve at HIGH instead of hard-blocking.
            if "sudo " in text or _looks_like_password(text):
                return RISK_HIGH
        return risk
    if tool_name == "macos":
        risk = _MACOS_RISK.get(action, RISK_HIGH)
        if action == "safe_shell":
            cmd = (tool_input.get("command") or "").strip()
            if cmd.startswith("sudo ") or " sudo " in f" {cmd} ":
                return RISK_BLOCKED
        if action == "run_applescript":
            script = (tool_input.get("script") or "").lower()
            if "do shell script" in script and "with administrator privileges" in script:
                return RISK_BLOCKED
        return risk
    return RISK_HIGH


def _looks_like_password(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 64 or " " in stripped or "\n" in stripped:
        return False
    has_alpha = any(c.isalpha() for c in stripped)
    has_other = any(not c.isalnum() for c in stripped) or any(c.isdigit() for c in stripped)
    return has_alpha and has_other and len(stripped) >= 8


@dataclass
class ReviewPolicy:
    mode: str = "high_risk"
    require_final_review: bool = True
    assume_yes: bool = False
    allow_typing_anything: bool = False
    review_dir: Optional[Path] = None
    vscode_hidden: bool = False
    approvals_log_md: Optional[Path] = None
    approvals_log_jsonl: Optional[Path] = None
    approved_once: set = field(default_factory=set)

    def __post_init__(self):
        if self.mode not in REVIEW_MODES:
            raise ValueError(f"invalid review mode: {self.mode}")

    def attach(self, run_dir: Path) -> None:
        self.review_dir = run_dir / "review"
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.approvals_log_md = self.review_dir / "approvals.md"
        self.approvals_log_jsonl = self.review_dir / "approvals.jsonl"
        if not self.approvals_log_md.exists():
            self.approvals_log_md.write_text("# Approvals log\n\n")

    def preflight(self) -> tuple[bool, str]:
        """B1 — call BEFORE VSCode hides. Probes osascript TCC automation and
        prints user-facing instructions if it fails. Never raises; returns
        (ok, message) so the caller can also log it."""
        ok, msg = preflight_tcc()
        if ok:
            return True, msg
        print(
            "\n[TCC preflight] osascript automation check failed: "
            f"{msg}\n"
            "      The per-action approval dialog may pop BEHIND VSCode and time out silently.\n"
            "      To fix it permanently:\n"
            "        System Settings → Privacy & Security → Automation → <your terminal>\n"
            "          → enable 'System Events'.\n"
            "      The run will proceed and fall back to file-based approvals "
            "(pending-*.json) if needed — use `--approve-pending RUN_DIR` to clear them.\n",
            flush=True,
        )
        return False, msg

    def should_gate(self, risk: str) -> bool:
        if risk == RISK_BLOCKED:
            return True
        if self.mode == "off":
            return False
        if self.mode == "plan":
            return False
        if self.mode == "high_risk":
            return risk == RISK_HIGH
        if self.mode == "every_action":
            return risk != RISK_OBSERVE
        return False


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, AttributeError):
        return False


def _osascript_available() -> bool:
    return shutil.which("osascript") is not None


_TCC_PROBED = False


def probe_tcc_automation(timeout_s: int = 6) -> tuple[bool, str]:
    """B1 — probe macOS System Events automation permission.

    On a fresh interpreter identity, the first call triggers the TCC prompt.
    If the user already denied it, returncode is non-zero with a message like
    'Not authorized to send Apple events to System Events.'
    """
    if not _osascript_available():
        return False, "osascript not found"
    try:
        proc = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name'],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "osascript probe timed out (likely waiting for TCC dialog response)"
    if proc.returncode == 0:
        return True, "ok"
    return False, (proc.stderr or proc.stdout or "unknown error").strip()


def preflight_tcc(*, force: bool = False) -> tuple[bool, str]:
    """B1 — run the TCC probe once per process. Returns (ok, message)."""
    global _TCC_PROBED
    if _TCC_PROBED and not force:
        return True, "already-probed-this-process"
    _TCC_PROBED = True
    return probe_tcc_automation()


def _ask_terminal(prompt: str, choices: str) -> str:
    sys.stdout.write(f"\n{prompt}\n{choices}: ")
    sys.stdout.flush()
    try:
        return (sys.stdin.readline() or "").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return "a"


def _ask_dialog(message: str, buttons=("Reject", "Approve"), default="Approve", timeout_s: int = 120) -> Optional[str]:
    if not _osascript_available():
        return None
    btns = ", ".join(f'"{b}"' for b in buttons)
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'with timeout of {timeout_s} seconds\n'
        f'  display dialog "{safe_message}" buttons {{{btns}}} '
        f'default button "{default}" with title "coa-test review"\n'
        f'end timeout'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout_s + 10,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    for b in buttons:
        if f"button returned:{b}" in out:
            return b
    return None


def _log_decision(policy: ReviewPolicy, entry: dict) -> None:
    if policy.approvals_log_jsonl is None or policy.approvals_log_md is None:
        return
    with policy.approvals_log_jsonl.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    with policy.approvals_log_md.open("a") as f:
        f.write(
            f"## Step {entry.get('step', '?')} — {entry.get('decision', '?').upper()} "
            f"({entry.get('risk', '?')})\n"
            f"- tool: `{entry.get('tool')}.{entry.get('action', '?')}`\n"
            f"- input: `{entry.get('input')}`\n"
            f"- reviewer: {entry.get('reviewer')}\n"
            f"- timestamp: {entry.get('timestamp')}\n"
            f"- screenshot: {entry.get('screenshot') or '(none)'}\n\n"
        )


def request_plan_approval(policy: ReviewPolicy, plan_text: str, task: str) -> tuple[bool, str]:
    """Gate A — pre-run plan approval. Always over stdin; called before VSCode hides."""
    if policy.review_dir is not None:
        (policy.review_dir / "plan.md").write_text(
            f"# Plan ({_ts()})\n\n**Task:** {task}\n\n{plan_text}\n"
        )
    if policy.mode == "off":
        _log_decision(policy, {
            "step": 0, "risk": "plan", "tool": "(plan)", "action": "approve",
            "decision": "approved-auto", "reviewer": "policy-off",
            "timestamp": _ts(), "screenshot": None, "input": None,
        })
        return True, "review mode is off"
    if policy.assume_yes:
        _log_decision(policy, {
            "step": 0, "risk": "plan", "tool": "(plan)", "action": "approve",
            "decision": "approved-auto", "reviewer": "assume-yes",
            "timestamp": _ts(), "screenshot": None, "input": None,
        })
        return True, "assume-yes"

    print("\n=== Proposed plan ===")
    print(plan_text)
    print("=====================")
    if _stdin_is_tty():
        ans = _ask_terminal("Approve plan?", "[y]es / [a]bort")
        approved = ans.startswith("y")
        reason = "stdin"
    else:
        choice = _ask_dialog(f"Approve the proposed plan?\n\nTask: {task[:200]}")
        approved = choice == "Approve"
        reason = f"dialog:{choice}"
    _log_decision(policy, {
        "step": 0, "risk": "plan", "tool": "(plan)", "action": "approve",
        "decision": "approved" if approved else "rejected",
        "reviewer": reason, "timestamp": _ts(), "screenshot": None, "input": None,
    })
    return approved, reason


def require_approval(
    policy: ReviewPolicy,
    step: int,
    tool_name: str,
    tool_input: dict,
    screenshot_path: Optional[str],
    last_screenshot_png: Optional[Path] = None,
) -> tuple[bool, str, str]:
    """Gate B. Returns (approved, risk, reason). Caller is expected to skip
    execution and surface `reason` to the model when approved is False."""
    risk = classify_risk(tool_name, tool_input, allow_typing_anything=policy.allow_typing_anything)
    action = (tool_input or {}).get("action", "?")

    if risk == RISK_BLOCKED:
        entry = {
            "step": step, "risk": risk, "tool": tool_name, "action": action,
            "input": tool_input, "decision": "blocked",
            "reviewer": "policy", "timestamp": _ts(), "screenshot": screenshot_path,
        }
        _log_decision(policy, entry)
        return False, risk, "BLOCKED by policy (sudo/password/destructive)."

    if not policy.should_gate(risk):
        return True, risk, "auto-approved"

    summary = f"step {step}: {tool_name}.{action} ({risk}) — {_short_input(tool_input)}"

    if policy.assume_yes and risk != RISK_HIGH:
        _log_decision(policy, {
            "step": step, "risk": risk, "tool": tool_name, "action": action,
            "input": tool_input, "decision": "approved-auto",
            "reviewer": "assume-yes", "timestamp": _ts(), "screenshot": screenshot_path,
        })
        return True, risk, "assume-yes"

    if not policy.vscode_hidden and _stdin_is_tty():
        ans = _ask_terminal(f"Approve {summary}?", "[y]es / [n]o")
        approved = ans.startswith("y")
        reviewer = "stdin"
    else:
        choice = _ask_dialog(
            f"Approve {tool_name}.{action} (risk: {risk})?\n\n{_short_input(tool_input)}"
        )
        if choice is None:
            return _file_fallback(policy, step, tool_name, action, tool_input, risk, screenshot_path, last_screenshot_png)
        approved = choice == "Approve"
        reviewer = f"dialog:{choice}"

    _log_decision(policy, {
        "step": step, "risk": risk, "tool": tool_name, "action": action,
        "input": tool_input,
        "decision": "approved" if approved else "rejected",
        "reviewer": reviewer, "timestamp": _ts(), "screenshot": screenshot_path,
    })
    if approved:
        return True, risk, "approved"
    return False, risk, f"REJECTED by human reviewer ({reviewer})."


def _file_fallback(
    policy: ReviewPolicy, step: int, tool_name: str, action: str,
    tool_input: dict, risk: str, screenshot_path: Optional[str],
    last_screenshot_png: Optional[Path],
) -> tuple[bool, str, str]:
    if policy.review_dir is None:
        return False, risk, "BLOCKED: no review dir to write pending request."
    pending_json = policy.review_dir / f"pending-{step:03d}.json"
    pending_json.write_text(json.dumps({
        "step": step, "tool": tool_name, "action": action,
        "input": tool_input, "risk": risk, "screenshot": screenshot_path,
        "timestamp": _ts(),
    }, indent=2))
    if last_screenshot_png is not None and last_screenshot_png.exists():
        shutil.copy(last_screenshot_png, policy.review_dir / f"pending-{step:03d}.png")
    _log_decision(policy, {
        "step": step, "risk": risk, "tool": tool_name, "action": action,
        "input": tool_input, "decision": "pending",
        "reviewer": "file-fallback", "timestamp": _ts(),
        "screenshot": screenshot_path,
    })
    return False, risk, (
        f"BLOCKED pending human review. See {pending_json.name}; "
        "approve out-of-band and rerun this action, or abort the run."
    )


def approve_pending(
    run_dir: Path,
    *,
    asker=None,
    reviewer: str = "approve-pending",
) -> dict:
    """B2 — clear file-fallback BLOCKED pendings out-of-band.

    Walks `<run_dir>/review/pending-*.json`, asks the human for each decision,
    writes a sibling `pending-NNN.decision.json`, and appends an entry to the
    run's `approvals.jsonl` + `approvals.md`. Files that already have a
    `.decision.json` are skipped, so this is safe to re-run.

    Returns `{"reviewed", "approved", "rejected", "skipped"}` counts.
    """
    review_dir = run_dir / "review"
    if not review_dir.is_dir():
        raise FileNotFoundError(f"no review/ subfolder in {run_dir}")
    asker = asker or _default_pending_asker
    counts = {"reviewed": 0, "approved": 0, "rejected": 0, "skipped": 0}

    approvals_md = review_dir / "approvals.md"
    approvals_jsonl = review_dir / "approvals.jsonl"
    if not approvals_md.exists():
        approvals_md.write_text("# Approvals log\n\n")

    for pending_path in sorted(review_dir.glob("pending-*.json")):
        if pending_path.name.endswith(".decision.json"):
            continue
        decision_path = pending_path.with_suffix(".decision.json")
        if decision_path.exists():
            counts["skipped"] += 1
            continue
        try:
            pending = json.loads(pending_path.read_text())
        except (json.JSONDecodeError, OSError):
            counts["skipped"] += 1
            continue

        png_path = pending_path.with_suffix(".png")
        png_hint = f" (screenshot: {png_path.name})" if png_path.exists() else ""
        summary = (
            f"step {pending.get('step')}: {pending.get('tool')}.{pending.get('action')} "
            f"({pending.get('risk')}) — {_short_input(pending.get('input', {}))}{png_hint}"
        )
        decision = asker(summary)
        counts["reviewed"] += 1
        if decision not in ("approve", "reject"):
            counts["skipped"] += 1
            continue

        entry = {
            "step": pending.get("step"),
            "risk": pending.get("risk"),
            "tool": pending.get("tool"),
            "action": pending.get("action"),
            "input": pending.get("input"),
            "decision": "approved" if decision == "approve" else "rejected",
            "reviewer": reviewer,
            "timestamp": _ts(),
            "screenshot": pending.get("screenshot"),
            "via": "approve-pending",
        }
        counts["approved" if decision == "approve" else "rejected"] += 1
        decision_path.write_text(json.dumps(entry, indent=2))
        with approvals_jsonl.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        with approvals_md.open("a") as f:
            f.write(
                f"## Step {entry['step']} — {entry['decision'].upper()} "
                f"({entry['risk']}) [out-of-band]\n"
                f"- tool: `{entry['tool']}.{entry['action']}`\n"
                f"- input: `{entry['input']}`\n"
                f"- reviewer: {entry['reviewer']}\n"
                f"- timestamp: {entry['timestamp']}\n"
                f"- screenshot: {entry['screenshot'] or '(none)'}\n\n"
            )
    return counts


def _default_pending_asker(summary: str) -> str:
    print("\n=== Pending review ===")
    print(summary)
    ans = _ask_terminal("Decision?", "[a]pprove / [r]eject / [s]kip")
    return {"a": "approve", "r": "reject"}.get(ans[:1], "skip")


def _short_input(tool_input: dict, limit: int = 220) -> str:
    try:
        s = json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(tool_input)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def write_final_report(
    rec_dir: Path,
    *,
    task: str,
    agent_claim: str,
    machine_verification: dict,
    transcript_name: str,
    screenshots: list,
    approvals_path: Optional[str],
    human_decision: Optional[dict] = None,
) -> tuple[Path, Path]:
    """Gate C — always-written final report, regardless of pass/fail/blocked."""
    status = _derive_status(agent_claim, machine_verification, human_decision)
    report = {
        "task": task,
        "agent_claim": agent_claim,
        "machine_verification": machine_verification,
        "status": status,
        "transcript": transcript_name,
        "screenshots": screenshots,
        "approvals": approvals_path,
        "human_final_review": human_decision,
        "generated_at": _ts(),
    }
    json_path = rec_dir / "final_report.json"
    md_path = rec_dir / "final_report.md"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(_render_md(report))
    return md_path, json_path


def _derive_status(agent_claim: str, machine: dict, human: Optional[dict]) -> str:
    if human and human.get("decision"):
        return human["decision"]
    if not machine:
        return "needs_review"
    if machine.get("blocked"):
        return "blocked"
    if machine.get("ok") is True and agent_claim == "TASK COMPLETE":
        return "needs_review"  # no human signed off yet
    if machine.get("ok") is False:
        return "fail"
    return "needs_review"


def _render_md(report: dict) -> str:
    lines = [
        f"# Final report — {report['generated_at']}",
        "",
        f"**Task:** {report['task']}",
        "",
        f"**Agent claim:** `{report['agent_claim']}`",
        "",
        f"**Status:** `{report['status']}`",
        "",
        "## Machine verification",
        "",
        "```json",
        json.dumps(report["machine_verification"], indent=2),
        "```",
        "",
        "## Human final review",
        "",
        "```json",
        json.dumps(report["human_final_review"], indent=2),
        "```",
        "",
        f"**Transcript:** `{report['transcript']}`  ",
        f"**Approvals:** `{report['approvals']}`  ",
        f"**Screenshots ({len(report['screenshots'])}):**",
        "",
    ]
    for s in report["screenshots"]:
        lines.append(f"- `{s}`")
    return "\n".join(lines) + "\n"


def request_final_review(policy: ReviewPolicy, summary: str) -> dict:
    """Gate C interactive part. Returns the human_final_review dict.

    Note: --review-mode off only disables Gate A (plan) and Gate B (per-action).
    Gate C still runs unless --no-final-review is passed (require_final_review=False).
    """
    if not policy.require_final_review:
        return {"decision": None, "notes": "skipped (require_final_review=False)", "timestamp": _ts()}
    if policy.assume_yes:
        return {"decision": "pass", "notes": "auto (assume-yes)", "timestamp": _ts()}

    print("\n=== Final evidence review ===")
    print(summary)
    print("=============================")
    if _stdin_is_tty() and not policy.vscode_hidden:
        ans = _ask_terminal("Mark run as", "[p]ass / [f]ail / [n]eeds-review")
        decision = {"p": "pass", "f": "fail", "n": "needs_review"}.get(ans[:1], "needs_review")
        reviewer = "stdin"
    else:
        choice = _ask_dialog(
            f"Mark run result:\n\n{summary[:400]}",
            buttons=("fail", "needs_review", "pass"),
            default="pass",
            timeout_s=300,
        )
        decision = choice or "needs_review"
        reviewer = f"dialog:{choice}"
    return {"decision": decision, "notes": reviewer, "timestamp": _ts()}
