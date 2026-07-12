#!/usr/bin/env bash
# demo.sh — run the COA measurement flows back-to-back for a single screen recording.
# Beat-by-beat narration runbook: docs/DEMO.md. Start your screen recorder, then run this.
#
#   ./demo.sh                both classic flows + report (default), with narration pauses
#   ./demo.sh --tradingview  only Flow #1 (browser, machine-scored)
#   ./demo.sh --proton       only Flow #3 (no_api, human-gated — click "pass")
#   ./demo.sh --github       only Flow #2 (multi_app: GitHub PRs + Proton invoices → Notion).
#                            OPEN PR(s) ON THE REPO FIRST — those live PRs are the dynamic input;
#                            have a few invoice/billing emails in the Proton inbox. No extra keys.
#   ./demo.sh --no-pause     skip the narration pauses (unattended)
#
# Notes:
#   * Run from Terminal.app / iTerm (NOT the VSCode terminal — the runner hides VSCode).
#   * Needs Screen Recording + Accessibility + Automation permissions (docs/ARCHITECTURE.md §13).
#   * Abort any run by slamming the cursor into a screen corner (pyautogui FAILSAFE).
#   * Override the interpreter with PYTHON=... (defaults to `python`, falls back to `python3`).

set -uo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY="python3"

RUN_TV=1; RUN_PR=1; RUN_GH=0; PAUSE=1
for arg in "$@"; do
  case "$arg" in
    --tradingview) RUN_TV=1; RUN_PR=0; RUN_GH=0 ;;
    --proton)      RUN_TV=0; RUN_PR=1; RUN_GH=0 ;;
    --github)      RUN_TV=0; RUN_PR=0; RUN_GH=1 ;;
    --no-pause)    PAUSE=0 ;;
    -h|--help)     grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $arg (try --help)"; exit 2 ;;
  esac
done

bar()  { printf '\n\033[1m======== %s ========\033[0m\n' "$1"; }
beat() {  # narration pause — skipped under --no-pause / non-interactive
  [ "$PAUSE" -eq 1 ] || return 0
  printf '\n\033[2m%s\033[0m\n' "$1"
  read -r -p "  press Enter to continue… " _ || true
}

bar "preflight — screen / keyboard / automation permissions"
$PY -u agent.py --diagnose || echo "(diagnose reported issues — see runs/<ts>/; continuing anyway)"
beat "Permissions look OK. We'll run two deterministic flows and measure every run."

if [ "$RUN_TV" -eq 1 ]; then
  bar "Flow #1 — TradingView (browser · machine-verified)"
  beat "Brave → SPX chart → Daily → 4H → read the close → flag TA patterns. The oracle checks that read close against an INDEPENDENT market quote, not the chart it just looked at."
  $PY -u runner.py flows/tradingview.yaml || echo "(runner exited non-zero; continuing)"
  bar "regenerating the dashboard"
  $PY report.py
  beat "STATUS: pass = the agent's close matched the independent quote within ±0.5%."
fi

if [ "$RUN_GH" -eq 1 ]; then
  bar "Flow #2 — GitHub PRs + Proton invoices → Notion (multi_app · machine + human)"
  beat "PRs were JUST opened on the repo — the agent doesn't know their numbers or titles. It reads every open PR in Brave and logs one row each to the Notion 'COA test' database, then reads the invoice/billing emails in Proton and logs those too — two systems consolidated into one tracker. The oracle machine-verifies every PR read against the GitHub API; you confirm the Notion rows and invoices."
  $PY -u runner.py flows/github_pr.yaml --max-step-actions 30 || echo "(runner exited non-zero; continuing)"
  bar "regenerating the dashboard"
  $PY report.py
  beat "STATUS: pass = every PR the agent read matched GitHub's record AND you approved the Notion rows + invoices."
fi

if [ "$RUN_PR" -eq 1 ]; then
  bar "Flow #3 — Proton Mail (no_api · human-gated)"
  beat "No API to verify a mailbox → the HUMAN is the oracle. The agent marks the top-5 emails read, then an approval dialog pops — click PASS. It still logs real cost/tokens/time."
  $PY -u runner.py flows/proton.yaml || echo "(runner exited non-zero; continuing)"
  bar "regenerating the dashboard"
  $PY report.py
fi

bar "done — opening the dashboard"
open docs/results.html 2>/dev/null || echo "open docs/results.html to view the metrics"
