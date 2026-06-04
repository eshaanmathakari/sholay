"""Minimal REPL for driving the agent without baking the task into a CLI arg.

`agent.py main()` routes here when no positional task and no short-circuit flag
(`--diagnose`, `--approve-pending`) is given. The REPL itself contains no
business logic — it dispatches to callables the caller provides so flags from
the invocation (`--review-mode`, `--allow-typing-anything`, etc.) are honored
without reparsing argv here.
"""
import json
from pathlib import Path
from typing import Callable, Optional

import review


def list_recent_runs(runs_dir: Path, n: int = 5) -> list[tuple[str, str, str]]:
    """Returns [(timestamp_dir_name, status, task)] for the N most recent runs.

    Looks for `final_report.json` first, then falls back to flagging diagnostics
    runs. Unreadable / partial dirs surface as `(no report)` rather than crashing.
    """
    if not runs_dir.is_dir():
        return []
    rows = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        status, task = _read_final_report(run_dir)
        rows.append((run_dir.name, status, task))
        if len(rows) >= n:
            break
    return rows


def _read_final_report(run_dir: Path) -> tuple[str, str]:
    fr = run_dir / "final_report.json"
    if not fr.exists():
        if (run_dir / "diagnostics.json").exists():
            return ("diag", "(diagnostics)")
        return ("(no report)", "")
    try:
        data = json.loads(fr.read_text())
        return data.get("status", "?"), (data.get("task") or "")[:80]
    except (json.JSONDecodeError, OSError):
        return ("(unreadable)", "")


def _print_recent_runs(runs_dir: Path, n: int, output_fn: Callable[[str], None]) -> None:
    rows = list_recent_runs(runs_dir, n=n)
    if not rows:
        output_fn(f"\nrecent runs: (none in {runs_dir}/)")
        return
    output_fn("\nrecent runs:")
    for ts, status, task in rows:
        output_fn(f"  {ts}  {status:<14} {task or '—'}")


def _print_help(output_fn: Callable[[str], None]) -> None:
    output_fn(
        "\nREPL commands:\n"
        "  <task text>             run the task with the current policy\n"
        "  /diagnose               run preflight diagnostics (no API calls)\n"
        "  /approve-pending <ts>   review BLOCKED pendings in runs/<ts>/\n"
        "  /list                   re-print recent runs\n"
        "  /help                   this message\n"
        "  /quit  (or  quit  or Ctrl+D)"
    )


def run_repl(
    *,
    runs_dir: Path = Path("runs"),
    policy_factory: Callable[[], review.ReviewPolicy],
    run_task_callable: Callable[..., None],
    run_diagnose_callable: Optional[Callable[[], None]] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    recent_n: int = 5,
) -> None:
    """Run the REPL until the user quits.

    `policy_factory` is called once per task so flags like `mode` and
    `allow_typing_anything` from the original invocation are reapplied (you
    can't change them mid-REPL by design — keep this surface tiny).
    """
    sample = policy_factory()
    output_fn(
        f"\ncoa-test REPL · review mode: {sample.mode} · "
        f"final-review: {sample.require_final_review} · "
        f"typing-anything: {sample.allow_typing_anything}"
    )
    _print_recent_runs(runs_dir, recent_n, output_fn)
    output_fn("\nType a task or /help. Ctrl+D or 'quit' to exit.")

    while True:
        try:
            line = input_fn("\ntask> ").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("\nbye.")
            return

        if not line:
            continue
        if line in ("quit", "/quit", "exit", ":q"):
            output_fn("bye.")
            return
        if line == "/help":
            _print_help(output_fn)
            continue
        if line == "/list":
            _print_recent_runs(runs_dir, recent_n, output_fn)
            continue
        if line.startswith("/diagnose"):
            if run_diagnose_callable is None:
                output_fn("(diagnose not wired; pass run_diagnose_callable)")
            else:
                run_diagnose_callable()
            continue
        if line.startswith("/approve-pending"):
            parts = line.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                output_fn("usage: /approve-pending <run-ts>")
                continue
            target = runs_dir / parts[1].strip()
            if not target.is_dir():
                output_fn(f"(no such run dir: {target})")
                continue
            counts = review.approve_pending(target)
            output_fn(
                f"reviewed {counts['reviewed']}: "
                f"{counts['approved']} approved, {counts['rejected']} rejected, "
                f"{counts['skipped']} skipped"
            )
            continue
        if line.startswith("/"):
            output_fn(f"(unknown command: {line.split()[0]}; try /help)")
            continue

        policy = policy_factory()
        try:
            run_task_callable(line, policy=policy)
        except KeyboardInterrupt:
            output_fn("\n(task aborted by Ctrl+C; back at prompt)")
