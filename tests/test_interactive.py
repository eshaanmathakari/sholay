"""REPL acceptance tests — empty input loops, quit exits, run_task fires once per task."""
import json
from unittest.mock import MagicMock

import pytest

import interactive
import review


def _inputs(lines):
    """Returns an input_fn that yields the given lines, then raises EOFError."""
    it = iter(lines)
    def _fn(_prompt=""):
        try:
            return next(it)
        except StopIteration as e:
            raise EOFError() from e
    return _fn


def _policy_factory():
    return lambda: review.ReviewPolicy(mode="high_risk", assume_yes=True)


def test_quit_exits_cleanly(tmp_path, capsys):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["quit"]),
    )
    assert run_task.call_count == 0
    assert "bye" in capsys.readouterr().out


def test_eof_exits_cleanly(tmp_path):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs([]),  # immediate EOF
    )
    assert run_task.call_count == 0


def test_empty_input_loops_back(tmp_path):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["", "   ", "quit"]),
    )
    assert run_task.call_count == 0


def test_task_dispatches_to_run_task_once(tmp_path):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["open the calculator app", "quit"]),
    )
    assert run_task.call_count == 1
    args, kwargs = run_task.call_args
    assert args[0] == "open the calculator app"
    assert "policy" in kwargs and isinstance(kwargs["policy"], review.ReviewPolicy)


def test_multiple_tasks_each_get_fresh_policy(tmp_path):
    """Per-task fresh policy: vscode_hidden / approved_once from task 1 must not leak into task 2."""
    seen_policies = []
    def _capture(_task, *, policy):
        seen_policies.append(policy)
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=_capture,
        input_fn=_inputs(["task one", "task two", "quit"]),
    )
    assert len(seen_policies) == 2
    assert seen_policies[0] is not seen_policies[1]


def test_help_command_does_not_dispatch_task(tmp_path, capsys):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["/help", "quit"]),
    )
    assert run_task.call_count == 0
    out = capsys.readouterr().out
    assert "/diagnose" in out
    assert "/approve-pending" in out


def test_unknown_slash_command_does_not_dispatch(tmp_path, capsys):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["/nope", "quit"]),
    )
    assert run_task.call_count == 0
    assert "unknown command" in capsys.readouterr().out


def test_diagnose_command_invokes_callable(tmp_path):
    run_task = MagicMock()
    diagnose = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        run_diagnose_callable=diagnose,
        input_fn=_inputs(["/diagnose", "quit"]),
    )
    assert diagnose.call_count == 1
    assert run_task.call_count == 0


def test_approve_pending_command_routes_to_review_module(tmp_path, monkeypatch):
    run_dir = tmp_path / "2026-05-26_12-00-00"
    (run_dir / "review").mkdir(parents=True)
    (run_dir / "review" / "pending-001.json").write_text(
        '{"step":1,"tool":"macos","action":"safe_shell","input":{"command":"x"},"risk":"high"}'
    )
    monkeypatch.setattr(review, "_ask_terminal", lambda *a, **kw: "a")  # approve

    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["/approve-pending 2026-05-26_12-00-00", "quit"]),
    )
    decision = json.loads((run_dir / "review" / "pending-001.decision.json").read_text())
    assert decision["decision"] == "approved"


def test_approve_pending_without_arg_prints_usage(tmp_path, capsys):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["/approve-pending", "quit"]),
    )
    assert "usage:" in capsys.readouterr().out
    assert run_task.call_count == 0


def test_approve_pending_missing_run_dir_prints_message(tmp_path, capsys):
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["/approve-pending nope", "quit"]),
    )
    assert "no such run dir" in capsys.readouterr().out


def test_list_recent_runs_reads_status_from_final_report(tmp_path):
    (tmp_path / "2026-05-25_10-00-00").mkdir()
    (tmp_path / "2026-05-25_10-00-00" / "final_report.json").write_text(
        json.dumps({"status": "pass", "task": "install gc12.dmg"})
    )
    (tmp_path / "2026-05-26_11-00-00").mkdir()
    (tmp_path / "2026-05-26_11-00-00" / "final_report.json").write_text(
        json.dumps({"status": "blocked", "task": "do something risky"})
    )
    (tmp_path / "2026-05-24_09-00-00").mkdir()  # no report and no diagnostics
    (tmp_path / "2026-05-23_08-00-00").mkdir()
    (tmp_path / "2026-05-23_08-00-00" / "diagnostics.json").write_text("{}")

    rows = interactive.list_recent_runs(tmp_path, n=5)
    assert len(rows) == 4
    assert rows[0] == ("2026-05-26_11-00-00", "blocked", "do something risky")
    assert rows[1] == ("2026-05-25_10-00-00", "pass", "install gc12.dmg")
    assert rows[2] == ("2026-05-24_09-00-00", "(no report)", "")
    assert rows[3] == ("2026-05-23_08-00-00", "diag", "(diagnostics)")


def test_list_recent_runs_empty_when_no_runs_dir(tmp_path):
    assert interactive.list_recent_runs(tmp_path / "nope") == []


def test_keyboard_interrupt_during_task_re_prompts(tmp_path, capsys):
    """Ctrl+C while a task is running should return to the prompt, not kill the REPL."""
    calls = {"n": 0}
    def _maybe_interrupt(_task, *, policy):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=_maybe_interrupt,
        input_fn=_inputs(["first", "second", "quit"]),
    )
    assert calls["n"] == 2  # second task still ran after interrupt
    assert "aborted" in capsys.readouterr().out


def test_list_command_re_prints_runs(tmp_path, capsys):
    (tmp_path / "2026-05-26_11-00-00").mkdir()
    (tmp_path / "2026-05-26_11-00-00" / "final_report.json").write_text(
        json.dumps({"status": "pass", "task": "x"})
    )
    run_task = MagicMock()
    interactive.run_repl(
        runs_dir=tmp_path,
        policy_factory=_policy_factory(),
        run_task_callable=run_task,
        input_fn=_inputs(["/list", "quit"]),
    )
    out = capsys.readouterr().out
    assert out.count("2026-05-26_11-00-00") >= 2  # printed at startup AND on /list
