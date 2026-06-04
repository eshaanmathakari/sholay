import json
from pathlib import Path

import pytest

import review


def test_classify_risk_high_install():
    assert review.classify_risk("macos", {"action": "install_app_from_dmg"}) == review.RISK_HIGH


def test_classify_risk_blocks_sudo_in_shell():
    risk = review.classify_risk("macos", {"action": "safe_shell", "command": "sudo ls"})
    assert risk == review.RISK_BLOCKED


def test_classify_risk_high_password_typing():
    """B3: password-like strings demote to HIGH (gateable), not BLOCKED.
    License keys, API tokens, "MyEmail123" all used to trip the BLOCKED path."""
    risk = review.classify_risk("computer", {"action": "type", "text": "MyP@ssw0rd!"})
    assert risk == review.RISK_HIGH


def test_classify_risk_typing_anything_flag_drops_to_default():
    """B3: --allow-typing-anything skips the heuristic entirely, returning the default 'type' risk."""
    risk = review.classify_risk(
        "computer", {"action": "type", "text": "sk-ant-MyP@ssw0rd!"},
        allow_typing_anything=True,
    )
    assert risk == review.RISK_MEDIUM


def test_classify_risk_typing_sudo_still_gated():
    """B3: typed 'sudo ' demoted from BLOCKED to HIGH so user can approve it for legit terminal use."""
    risk = review.classify_risk("computer", {"action": "type", "text": "sudo softwareupdate -l\n"})
    assert risk == review.RISK_HIGH


def test_classify_risk_safe_shell_sudo_still_blocked():
    """B3: safe_shell sudo stays BLOCKED — that's a direct privilege escalation, not typing."""
    risk = review.classify_risk("macos", {"action": "safe_shell", "command": "sudo whoami"})
    assert risk == review.RISK_BLOCKED


def test_classify_risk_blocks_cmd_q():
    risk = review.classify_risk("computer", {"action": "key", "text": "cmd+q"})
    assert risk == review.RISK_BLOCKED


def test_classify_risk_observe_screenshot():
    assert review.classify_risk("computer", {"action": "screenshot"}) == review.RISK_OBSERVE


def test_should_gate_high_risk_mode():
    p = review.ReviewPolicy(mode="high_risk")
    assert p.should_gate(review.RISK_HIGH) is True
    assert p.should_gate(review.RISK_MEDIUM) is False
    assert p.should_gate(review.RISK_BLOCKED) is True


def test_should_gate_every_action_excludes_observe():
    p = review.ReviewPolicy(mode="every_action")
    assert p.should_gate(review.RISK_LOW) is True
    assert p.should_gate(review.RISK_OBSERVE) is False


def test_should_gate_off_still_blocks():
    p = review.ReviewPolicy(mode="off")
    assert p.should_gate(review.RISK_HIGH) is False
    assert p.should_gate(review.RISK_BLOCKED) is True


def test_blocked_action_short_circuits(tmp_path, monkeypatch):
    p = review.ReviewPolicy(mode="off")
    p.attach(tmp_path)
    monkeypatch.setattr(review, "_ask_terminal", lambda *a, **kw: pytest.fail("should not prompt"))
    monkeypatch.setattr(review, "_ask_dialog", lambda *a, **kw: pytest.fail("should not prompt"))

    approved, risk, reason = review.require_approval(
        p, step=1, tool_name="macos",
        tool_input={"action": "safe_shell", "command": "sudo rm /"},
        screenshot_path=None,
    )
    assert approved is False
    assert risk == review.RISK_BLOCKED
    assert "BLOCKED" in reason


def test_file_fallback_writes_pending(tmp_path, monkeypatch):
    p = review.ReviewPolicy(mode="high_risk")
    p.attach(tmp_path)
    p.vscode_hidden = True  # force non-stdin path
    monkeypatch.setattr(review, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(review, "_ask_dialog", lambda *a, **kw: None)  # simulate no dialog

    approved, risk, reason = review.require_approval(
        p, step=7, tool_name="macos",
        tool_input={"action": "install_app_from_dmg", "dmg_path": "/x.dmg"},
        screenshot_path="007_install.png",
    )
    assert approved is False
    assert risk == review.RISK_HIGH
    assert "pending" in reason.lower()
    pending = tmp_path / "review" / "pending-007.json"
    assert pending.exists()
    data = json.loads(pending.read_text())
    assert data["tool"] == "macos"
    assert data["action"] == "install_app_from_dmg"


def test_approvals_log_records_decision(tmp_path, monkeypatch):
    p = review.ReviewPolicy(mode="high_risk")
    p.attach(tmp_path)
    monkeypatch.setattr(review, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(review, "_ask_terminal", lambda *a, **kw: "y")

    approved, _, _ = review.require_approval(
        p, step=3, tool_name="macos",
        tool_input={"action": "install_app_from_dmg"},
        screenshot_path="003_x.png",
    )
    assert approved is True
    jsonl = (tmp_path / "review" / "approvals.jsonl").read_text().strip().splitlines()
    entry = json.loads(jsonl[-1])
    assert entry["decision"] == "approved"
    assert entry["step"] == 3


def test_final_report_contains_required_fields(tmp_path):
    md, js = review.write_final_report(
        tmp_path,
        task="install gc12",
        agent_claim="TASK COMPLETE",
        machine_verification={"ok": True, "blocked": False, "rejected_actions": []},
        transcript_name="transcript.md",
        screenshots=["001_a.png", "002_b.png"],
        approvals_path="review/approvals.jsonl",
        human_decision={"decision": "pass", "notes": "looks good", "timestamp": "2026-05-24T11:31:00"},
    )
    assert md.exists() and js.exists()
    data = json.loads(js.read_text())
    assert data["agent_claim"] == "TASK COMPLETE"
    assert data["human_final_review"]["decision"] == "pass"
    assert data["machine_verification"]["ok"] is True
    body = md.read_text()
    assert "Final report" in body
    assert "001_a.png" in body


def test_review_mode_off_still_runs_final_review(monkeypatch):
    """B4: --review-mode off must NOT skip Gate C. Only --no-final-review skips it."""
    p = review.ReviewPolicy(mode="off", require_final_review=True)
    monkeypatch.setattr(review, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(review, "_ask_terminal", lambda *a, **kw: "p")

    decision = review.request_final_review(p, "summary text")
    assert decision["decision"] == "pass", "off-mode must still prompt for final review"


def test_no_final_review_skips_gate_c(monkeypatch):
    """B4: --no-final-review (require_final_review=False) is the only way to skip Gate C."""
    p = review.ReviewPolicy(mode="high_risk", require_final_review=False)
    monkeypatch.setattr(review, "_ask_terminal", lambda *a, **kw: pytest.fail("should not prompt"))
    monkeypatch.setattr(review, "_ask_dialog", lambda *a, **kw: pytest.fail("should not prompt"))

    decision = review.request_final_review(p, "summary")
    assert decision["decision"] is None
    assert "skipped" in decision["notes"]


def _reset_tcc_probed():
    review._TCC_PROBED = False


def test_probe_tcc_returns_ok_on_zero_returncode(monkeypatch):
    """B1: a returncode-0 osascript probe means System Events automation is approved."""
    class _Proc:
        returncode = 0
        stdout = "loginwindow\n"
        stderr = ""
    monkeypatch.setattr(review.subprocess, "run", lambda *a, **kw: _Proc())
    monkeypatch.setattr(review, "_osascript_available", lambda: True)

    ok, msg = review.probe_tcc_automation()
    assert ok is True
    assert msg == "ok"


def test_probe_tcc_returns_error_on_nonzero(monkeypatch):
    """B1: returncode != 0 surfaces stderr so the user sees the actual TCC denial message."""
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Not authorized to send Apple events to System Events. (-1743)\n"
    monkeypatch.setattr(review.subprocess, "run", lambda *a, **kw: _Proc())
    monkeypatch.setattr(review, "_osascript_available", lambda: True)

    ok, msg = review.probe_tcc_automation()
    assert ok is False
    assert "Not authorized" in msg


def test_probe_tcc_handles_timeout(monkeypatch):
    def _boom(*a, **kw):
        raise review.subprocess.TimeoutExpired(cmd="osascript", timeout=6)
    monkeypatch.setattr(review.subprocess, "run", _boom)
    monkeypatch.setattr(review, "_osascript_available", lambda: True)

    ok, msg = review.probe_tcc_automation()
    assert ok is False
    assert "timed out" in msg


def test_preflight_tcc_only_probes_once_per_process(monkeypatch):
    """B1: preflight_tcc memoizes so we don't spam osascript every gate."""
    _reset_tcc_probed()
    calls = []
    def _spy(*a, **kw):
        calls.append(1)
        class _P: returncode = 0; stdout = "ok"; stderr = ""
        return _P()
    monkeypatch.setattr(review.subprocess, "run", _spy)
    monkeypatch.setattr(review, "_osascript_available", lambda: True)

    review.preflight_tcc()
    review.preflight_tcc()
    review.preflight_tcc()
    assert len(calls) == 1


def test_policy_preflight_prints_instructions_on_failure(monkeypatch, capsys):
    """B1: when probe fails, the user must see clear TCC instructions BEFORE VSCode hides."""
    _reset_tcc_probed()
    monkeypatch.setattr(review, "probe_tcc_automation",
                        lambda **kw: (False, "Not authorized to send Apple events."))
    p = review.ReviewPolicy(mode="high_risk")

    ok, msg = p.preflight()
    out = capsys.readouterr().out
    assert ok is False
    assert "Privacy & Security" in out
    assert "Automation" in out
    assert "System Events" in out


def test_policy_preflight_silent_when_ok(monkeypatch, capsys):
    _reset_tcc_probed()
    monkeypatch.setattr(review, "probe_tcc_automation", lambda **kw: (True, "ok"))
    p = review.ReviewPolicy(mode="high_risk")

    ok, _ = p.preflight()
    assert ok is True
    assert capsys.readouterr().out == ""


def test_approve_pending_records_decision_and_appends_log(tmp_path):
    """B2: approve_pending walks pending-*.json, writes decision sidecars, and appends to approvals.jsonl."""
    p = review.ReviewPolicy(mode="high_risk")
    p.attach(tmp_path)
    pending = tmp_path / "review" / "pending-007.json"
    pending.write_text(json.dumps({
        "step": 7, "tool": "macos", "action": "install_app_from_dmg",
        "input": {"action": "install_app_from_dmg", "dmg_path": "/x.dmg"},
        "risk": "high", "screenshot": "007_install.png", "timestamp": "t",
    }))

    counts = review.approve_pending(tmp_path, asker=lambda s: "approve")

    assert counts == {"reviewed": 1, "approved": 1, "rejected": 0, "skipped": 0}
    decision = json.loads((tmp_path / "review" / "pending-007.decision.json").read_text())
    assert decision["decision"] == "approved"
    assert decision["via"] == "approve-pending"
    assert decision["step"] == 7
    jsonl = (tmp_path / "review" / "approvals.jsonl").read_text().strip().splitlines()
    assert json.loads(jsonl[-1])["decision"] == "approved"


def test_approve_pending_reject_path(tmp_path):
    p = review.ReviewPolicy(mode="high_risk")
    p.attach(tmp_path)
    (tmp_path / "review" / "pending-002.json").write_text(json.dumps({
        "step": 2, "tool": "macos", "action": "safe_shell",
        "input": {"action": "safe_shell", "command": "rm -rf /tmp/junk"},
        "risk": "high",
    }))

    counts = review.approve_pending(tmp_path, asker=lambda s: "reject")
    assert counts["rejected"] == 1
    decision = json.loads((tmp_path / "review" / "pending-002.decision.json").read_text())
    assert decision["decision"] == "rejected"


def test_approve_pending_idempotent_skips_decided(tmp_path):
    """B2: re-running approve_pending must NOT re-prompt for already-decided pendings."""
    p = review.ReviewPolicy(mode="high_risk")
    p.attach(tmp_path)
    (tmp_path / "review" / "pending-001.json").write_text(
        '{"step":1,"tool":"macos","action":"install_app_from_dmg","input":{},"risk":"high"}'
    )
    (tmp_path / "review" / "pending-001.decision.json").write_text('{"decision":"approved"}')

    counts = review.approve_pending(
        tmp_path, asker=lambda s: pytest.fail("should not prompt on second pass")
    )
    assert counts == {"reviewed": 0, "approved": 0, "rejected": 0, "skipped": 1}


def test_approve_pending_skip_choice_does_not_write_decision(tmp_path):
    """A 'skip' from the asker leaves the pending file undecided so the user can come back."""
    p = review.ReviewPolicy(mode="high_risk")
    p.attach(tmp_path)
    (tmp_path / "review" / "pending-003.json").write_text(
        '{"step":3,"tool":"macos","action":"safe_shell","input":{"command":"x"},"risk":"high"}'
    )

    counts = review.approve_pending(tmp_path, asker=lambda s: "skip")
    assert counts == {"reviewed": 1, "approved": 0, "rejected": 0, "skipped": 1}
    assert not (tmp_path / "review" / "pending-003.decision.json").exists()


def test_approve_pending_missing_review_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        review.approve_pending(tmp_path, asker=lambda s: "approve")


def test_final_report_status_blocked_when_rejection(tmp_path):
    md, js = review.write_final_report(
        tmp_path,
        task="x",
        agent_claim="BLOCKED",
        machine_verification={"ok": False, "blocked": True, "rejected_actions": [{"tool": "macos"}]},
        transcript_name="transcript.md",
        screenshots=[],
        approvals_path=None,
        human_decision={"decision": None, "notes": "skipped", "timestamp": "t"},
    )
    data = json.loads(js.read_text())
    assert data["status"] == "blocked"
