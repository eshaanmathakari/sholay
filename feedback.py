"""Capture human feedback on a run (capture-only — see docs/PLAN.md D7).

Writes a verdict (which may override the oracle) + free-text notes into the
`feedback` table, linked to a run. It never mutates the run's oracle status; the
human verdict lives alongside it. Humans then tune the YAML playbook by hand.

Usage:
    python feedback.py 2026-06-16_10-02-28 --fail --notes "flagged a pattern that isn't there"
    python feedback.py 2026-06-16_10-02-28          # interactive prompts
"""
import argparse
import os
import sys
from datetime import datetime

import metrics_db

VERDICTS = ("pass", "fail")


def record(conn, run_id: str, verdict: str, notes: str = "", reviewer: str = "human", ts: str = None) -> None:
    """Insert one feedback row. Raises ValueError on an invalid verdict."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    metrics_db.insert_feedback(conn, {
        "run_id": run_id,
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "reviewer": reviewer,
        "verdict": verdict,
        "notes": notes,
    })


def _run_exists(conn, run_id: str) -> bool:
    return conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description="Record human feedback on a run.")
    ap.add_argument("run_id", help="the run_id (the runs/<timestamp> dir name)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--pass", dest="verdict", action="store_const", const="pass")
    group.add_argument("--fail", dest="verdict", action="store_const", const="fail")
    ap.add_argument("--notes", default=None, help="free-text notes / corrections")
    ap.add_argument("--reviewer", default=os.environ.get("USER", "human"))
    ap.add_argument("--db", default=str(metrics_db.DEFAULT_DB))
    args = ap.parse_args()

    conn = metrics_db.connect(args.db)
    if not _run_exists(conn, args.run_id):
        print(f"warning: no run with run_id={args.run_id!r} in {args.db} (recording anyway)")

    verdict = args.verdict
    while verdict not in VERDICTS:
        verdict = input("verdict [pass/fail]: ").strip().lower()
    notes = args.notes if args.notes is not None else input("notes: ").strip()

    record(conn, args.run_id, verdict, notes=notes, reviewer=args.reviewer)
    conn.close()
    print(f"recorded {verdict!r} feedback for {args.run_id} by {args.reviewer}")


if __name__ == "__main__":
    sys.exit(main())
