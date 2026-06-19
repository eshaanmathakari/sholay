# coa-test — a review-gated macOS computer-use agent

A small agent that operates a real Mac the way a person does — it looks at the
screen, decides **one** action at a time, drives the cursor and keyboard, and
records every step. Three human-in-the-loop checkpoints sit between the model
and the machine so a person can intervene **before**, **during**, and **after**
each run.

Built on [Anthropic's Computer Use API](https://docs.anthropic.com/en/docs/build-with-claude/computer-use).
Deliberately small and **desktop-native** (not web): open apps, type into native
forms, navigate Finder, install from a `.dmg`, play local media. The only
frontend is a terminal REPL.

> **Two ways to read this repo:**
> - 📄 **Visual brief** — open [docs/brief.html](docs/brief.html) in a browser for
>   an illustrated overview + an interactive architecture flowchart whose boxes
>   link straight to the source files.
> - 📓 **Engineering notes** — [docs/NOTES.md](docs/NOTES.md) is the deep dive:
>   every gate, the risk taxonomy, the escape hatch, known limits.

> **Built on top: deterministic measurement flows.** A second layer runs *predefined* flows
> through the same agent and **measures every run** for cost, accuracy, and tokens across app
> types ([see below](#coa-measurement-flows)). The manager-facing artifacts:
> - 🎛 **Visual brief** — [docs/flows.html](docs/flows.html) (tabbed overview + interactive, clickable architecture diagrams — a twin of the agent's brief)
> - 📊 **Live metrics dashboard** — [docs/results.html](docs/results.html)
> - 🏗 **Architecture & demo guide** — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
> - 📋 **Design & decisions** — [docs/PLAN.md](docs/PLAN.md)

---

## How it works, in one picture

```
human types a task
      │
      ▼
[ Gate A ]  optional plan approval  ──► reject ► stop
      │ approve
      ▼
hide editor ─► screenshot ─► Claude picks ONE tool call
      │                            │
      │                            ▼
      │                    [ Gate B ] classify risk
      │                       observe/low/med ─► run
      │                       high ─► ask human ─► run / reject
      │                       blocked ─► return error to model (never runs)
      │                            │
      │                    execute ─► screenshot ─► record
      └──────────◄ loop until "TASK COMPLETE" or max 80 steps
      ▼
[ Gate C ]  post-run evidence review ─► human marks pass / fail / needs-review
      ▼
runs/<timestamp>/  (screenshots · transcript · approvals log · final report)
```

The gates are **enforced in code**, not just requested in the system prompt.

---

## Quick start

Requires **macOS** and **Python 3.10+**. (Mouse/keyboard control and
`screencapture` are macOS-specific.)

```bash
# 1. install deps into a venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. add your API key
cp .env.example .env          # then edit .env and paste your sk-ant-... key

# 3. preflight — no API calls, no charges, safe anytime
.venv/bin/python agent.py --diagnose

# 4. interactive REPL
.venv/bin/python -u agent.py

# …or a one-shot scripted run (-y auto-approves low/medium gates; high still asks)
.venv/bin/python -u agent.py -y "open Calculator and compute 47 * 23"
```

### One-time macOS permissions

Run from **Terminal.app or iTerm — not the VSCode integrated terminal** (the
agent hides VSCode mid-run, which would hide any prompt printed there).

In **System Settings → Privacy & Security**, grant your terminal app three permissions
— the agent's eyes, hands, and ability to talk to apps. **No `sudo` / admin is needed:**

| Permission | Role | Needed for |
|---|---|---|
| **Screen Recording** | eyes | `screencapture -x` — every step shoots the screen |
| **Accessibility** | hands | `pyautogui` mouse + keyboard control |
| **Automation (Apple Events)** | talk to apps | `osascript` activate / hide windows / `open location`; prompts **per app** |

`agent.py --diagnose` reports which of these are missing before any run.
`native_actions.safe_shell()` hard-refuses `sudo` and other destructive commands; the demo
flows install nothing. Full replication checklist (env, keys, gotchas):
**[docs/ARCHITECTURE.md §13](docs/ARCHITECTURE.md)**.

---

## Repository layout

All modules live in the repo root; one concern per file.

| File | Role |
|---|---|
| [agent.py](agent.py) | **Orchestrator.** Arg parsing, the `computer` + `macos` tool schemas, the planner call, the screenshot→model→gate→execute→record loop, and the final report. |
| [interactive.py](interactive.py) | **REPL.** Lists recent runs, dispatches `/diagnose`, `/approve-pending`, `/list`, `/help`, `/quit`. |
| [review.py](review.py) | **The three gates** + risk taxonomy + approvals log + file-fallback + `--approve-pending`. |
| [native_actions.py](native_actions.py) | **macOS host primitives** — `open_path`, `hide_vscode`, `install_app_from_dmg`, `eject_volume`, `verify_app_installed`, `run_applescript`, restricted `safe_shell`. |
| [screen.py](screen.py) | **Capture + downscale.** `screencapture` at Retina, resized to display points so model coordinates map 1:1 to the mouse. |
| [recorder.py](recorder.py) | **Artifact recorder.** Numbered screenshots + `transcript.md`, one folder per run. |
| [context_window.py](context_window.py) | **History compaction + prompt caching.** Keeps the N newest screenshots in API history (older ones become text refs, avoids 413 RequestTooLarge) and moves the rolling prompt-cache breakpoint to the latest turn so the stable prefix is billed as cache reads. |
| [diagnostics.py](diagnostics.py) | **Preflight** (`--diagnose`). Display size, TCC state, mounted DMGs. No Anthropic calls. |
| [tests/](tests/) | Agent suite + the flows framework's 64 offline tests (T1–T7); no live API calls. |
| [docs/](docs/) | [NOTES.md](docs/NOTES.md) (engineering notes), [brief.html](docs/brief.html) (visual brief), [design.md](docs/design.md) (the brief's design tokens). |

See [docs/NOTES.md](docs/NOTES.md#module-map) for the annotated module map with
line-level anchors.

---

## COA measurement flows

A second layer runs **predefined, written-down flows** through the same agent primitives and
**measures every run** — cost, accuracy, tokens — recording one row per run in SQLite. It inverts
the agent's open-ended autonomy into deterministic, scored flows across application *types*.

| File | Role |
|---|---|
| [runner.py](runner.py) | Drives a YAML playbook step-by-step through the agent loop; measures per-step tokens/time/actions; runs the oracle; writes a `runs.db` row + `final_report.json`. |
| [flows/loader.py](flows/loader.py) · [flows/](flows/) | Spec validation + the playbooks (`tradingview.yaml` browser flow, `proton.yaml` no-API flow). |
| [oracles/](oracles/) | One verifier per flow — `tradingview.py` (**machine**: independent quote, ±0.5%), `proton.py` (**human**: approval gate). |
| [metrics_db.py](metrics_db.py) · [pricing.py](pricing.py) | SQLite store (`runs`/`step_metrics`/`feedback`) · verified `claude-sonnet-4-6` `$/MTok` cost. |
| [report.py](report.py) · [feedback.py](feedback.py) | `runs.db` → CLI table + [docs/results.html](docs/results.html) (pass% · $/run · $/success · tokens · steps · **time/run** · a per-flow **step/time variability** chart) · capture a human verdict (may override the oracle). |
| [demo.sh](demo.sh) | Runs **both flows + report back-to-back** for a single screen recording (`--tradingview` / `--proton` / `--no-pause`). Runbook: [docs/DEMO.md](docs/DEMO.md). |

```bash
python -u runner.py flows/tradingview.yaml   # browser flow, machine-scored
python -u runner.py flows/proton.yaml         # no-API flow, human-gated (approval dialog)
python report.py                              # regenerate docs/results.html
python feedback.py <run_id> --pass --notes "…"
./demo.sh                                     # both flows + report, for a screen recording
```

**Adding a flow = drop a `flows/<name>.yaml` + an `oracles/<name>.py`** — no framework changes.
Full design in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/PLAN.md](docs/PLAN.md); the visual
brief is [docs/flows.html](docs/flows.html); the demo runbook is [docs/DEMO.md](docs/DEMO.md).

---

## The safety model in brief

Every tool call is classified before it runs. Whether a gate fires depends on
`--review-mode` (`off` / `plan` / `high_risk` (default) / `every_action`):

| Risk | Examples | `high_risk` (default) |
|---|---|---|
| observe | `screenshot`, `wait`, `verify_app_installed` | run |
| low | clicks, `scroll`, `open_path`, `hide_vscode` | run |
| medium | `type`, `key`, `left_click_drag`, `eject_volume` | run |
| **high** | `install_app_from_dmg`, `run_applescript`, `safe_shell` | **ask human** |
| **blocked** | `sudo` shells, admin AppleScript, `cmd+q` | **never runs** — returns an error to the model |

`blocked` actions never execute in any mode. Full taxonomy and the four review
modes are in [docs/NOTES.md](docs/NOTES.md#risk-taxonomy).

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q          # agent suite

# the flows framework's offline suites also run standalone (no pytest needed):
for t in tests/test_loader.py tests/test_pricing.py tests/test_metrics_db.py \
         tests/test_oracle_tradingview.py tests/test_oracle_proton.py \
         tests/test_feedback.py tests/test_report.py; do python3 "$t"; done
```

No live Anthropic calls — network fetches and approval dialogs are dependency-injected.

---

## Known limits

Desktop-only (no web UI), single task at a time, no mid-run resume after a
file-fallback block. The full list is in
[docs/NOTES.md](docs/NOTES.md#known-limits--non-goals).
