# coa-test — engineering notes

A small macOS computer-use agent built on Anthropic's Computer Use API. The
agent observes the screen via screenshots, decides one action at a time, drives
the cursor/keyboard, and records every step. Three human-in-the-loop gates sit
between the model and the host so a person can intervene before, during, and
after each run.

The project is deliberately small and non-web: opening apps, typing into native
forms, navigating Finder, installing from a `.dmg`, playing local media. There
is no web UI; the only frontend is a REPL.

---

## Module map

All modules live in the repo root; tests live in [tests/](../tests/).

| File | Role | Anchor |
|---|---|---|
| [agent.py](../agent.py) | Orchestrator. Parses args, builds the policy, runs the planner call, the tool loop, and the final report. Hosts the `computer` + `macos` tool schemas. | [`run_task`](../agent.py#L271) |
| [interactive.py](../interactive.py) | REPL. Lists recent runs, dispatches slash commands (`/diagnose`, `/approve-pending`, `/list`, `/help`, `/quit`), and routes task lines back to `run_task` via a callable. | [`run_repl`](../interactive.py#L67) |
| [screen.py](../screen.py) | `screencapture` wrapper. Resizes Retina pixels to display points before sending to Claude. Provides `image_block` / `text_block` helpers. | [`shoot`](../screen.py) |
| [recorder.py](../recorder.py) | Numbered screenshot saver + `transcript.md` writer. One folder per run under [runs/](../runs/). | — |
| [native_actions.py](../native_actions.py) | macOS host primitives: `open_path`, `hide_vscode`, `install_app_from_dmg`, `eject_volume`, `verify_app_installed`, `run_applescript`, `safe_shell`. | — |
| [context_window.py](../context_window.py) | `compact_messages` — keeps the N most recent image blocks in API history, replaces older ones with text references. Prevents 413 RequestTooLargeError. | — |
| [review.py](../review.py) | The three gates (plan / per-action / final report) + risk taxonomy + approvals log + file-fallback + TCC probe + `--approve-pending` subcommand. | [`ReviewPolicy`](../review.py#L100), [`classify_risk`](../review.py#L65) |
| [diagnostics.py](../diagnostics.py) | `--diagnose` preflight (no Anthropic calls). Prints display size, screencapture size, TCC state, mounted DMGs. | — |

---

## The three review gates

Enforced in code, not just in the system prompt.

**Gate A — pre-run plan approval** ([review.py:209](../review.py#L209))
The planner takes the initial screenshot + task, returns a numbered plan with
expected risk levels. Asked over stdin **before** VSCode hides, so the terminal
is still visible. Skipped when `--review-mode off`.

**Gate B — per-action approval** ([review.py:249](../review.py#L249))
Every tool call is classified by [`classify_risk`](../review.py#L65) into
`observe / low / medium / high / blocked`. Whether a gate fires depends on
`--review-mode`:

| Mode | Observe | Low | Medium | High | Blocked |
|---|---|---|---|---|---|
| `off` | run | run | run | run | block |
| `plan` | run | run | run | run | block |
| `high_risk` (default) | run | run | run | **ask** | block |
| `every_action` | run | **ask** | **ask** | **ask** | block |

When VSCode is hidden the gate prefers an AppleScript `display dialog`. If that
fails (TCC denied, no GUI, etc.) it falls back to writing `pending-NNN.json` +
`.png` and returning `BLOCKED pending human review` to the model — see the
escape hatch below.

**Gate C — post-run evidence review** ([review.py:425](../review.py#L425))
After the model ends its turn (or hits `MAX_STEPS`, or gets blocked), the
harness writes `final_report.{md,json}` and asks the human for a pass/fail/
needs-review decision. Skipped only when `--no-final-review` is passed.

> **Important semantic split** (fixed this session as B4): `--review-mode off`
> only disables Gates A and B. Gate C still runs unless `--no-final-review` is
> explicitly set.

---

## Risk taxonomy

Defined in [review.py:31](../review.py#L31). Highlights:

- `BLOCKED`: `safe_shell` with `sudo`, `run_applescript` with `with administrator privileges`, `cmd+q` / `command+q` keystrokes. These return a tool error to the model and never execute.
- `HIGH`: `install_app_from_dmg`, `run_applescript`, `safe_shell`. **Also** typed-text strings that look like passwords or contain `sudo ` (demoted from BLOCKED this session as B3 — they used to hard-block license keys and API tokens too).
- `MEDIUM`: `type`, `key`, `hold_key`, `left_click_drag`, `eject_volume`.
- `LOW`: clicks, scroll, mouse moves, `open_path`, `hide_vscode`, `activate_app`.
- `OBSERVE`: `screenshot`, `cursor_position`, `wait`, `verify_app_installed`.

Override the typed-text heuristic with `--allow-typing-anything` when you
genuinely need to type a long license key or API token.

---

## Running it

### Standalone Terminal, not VSCode

The agent hides VSCode/Electron before taking the first action so it isn't the
frontmost window. If you launch from the VSCode integrated terminal, any
text/prompt printed mid-run is hidden with VSCode. Always use **Terminal.app**
or **iTerm**.

### One-time TCC permissions

In **System Settings → Privacy & Security**, grant the terminal app:
- **Screen Recording** — for `screencapture`
- **Accessibility** — for `pyautogui` mouse + keyboard
- **Automation → System Events** — for AppleScript approval dialogs

If Automation is missing, the [B1 TCC probe](../review.py#L165) prints the
fix-it instructions before VSCode hides.

### Preflight (no API calls)

```bash
.venv/bin/python agent.py --diagnose
```

Writes `runs/<ts>/diagnostics.{md,json}`. Free; safe to run anytime.

### REPL (interactive)

```bash
.venv/bin/python -u agent.py
```

Lists recent runs with their `final_report.json` status, then `task>` prompt.
Slash commands inside the REPL:

- `/help` — list commands
- `/list` — re-print recent runs
- `/diagnose` — same as `--diagnose` but stays in the REPL
- `/approve-pending <ts>` — escape hatch (see below)
- `/quit` (or `quit`, or Ctrl+D) — exit

### Scripted (one-shot)

```bash
.venv/bin/python -u agent.py -y "open Calculator and compute 47 * 23"
```

`-y` skips the "Press Enter to start" prompt and auto-approves low/medium
gates; high-risk still prompts.

---

## CLI flags

| Flag | Default | When to use |
|---|---|---|
| `(positional task)` | — | One-shot scripted run. Omit to enter REPL. |
| `-y, --yes` | off | Skip "Press Enter" + assume-yes for low/medium gates |
| `--max-steps N` | 80 | Cap on tool calls per run |
| `--image-history N` | 4 | How many recent screenshots to keep in API history |
| `--no-hide-vscode` | off | Debug: keep VSCode visible (you'll see prompts in stderr) |
| `--diagnose` | off | Preflight only, no API calls, exit |
| `--diagnose-dmg PATH` | "" | Verify a specific DMG during `--diagnose` |
| `--diagnose-app NAME` | "" | Verify a specific installed app during `--diagnose` |
| `--review-mode MODE` | `high_risk` | `off` / `plan` / `high_risk` / `every_action` |
| `--review-plan` | off | Force Gate A even in `high_risk` mode |
| `--no-final-review` | off | Skip Gate C (the **only** way to skip it) |
| `--allow-typing-anything` | off | Skip the password/sudo-in-typed-text heuristic |
| `--approve-pending RUN_DIR` | — | Out-of-band approval for `pending-*.json` files, exit |

---

## The file-fallback escape hatch

When Gate B can't get a human answer (TCC denied, no GUI, dialog timeout), it
writes a `pending-NNN.json` + screenshot under `runs/<ts>/review/` and returns
`BLOCKED` to the model. The run terminates without that action ever executing.

To clear pendings out-of-band from another terminal:

```bash
.venv/bin/python agent.py --approve-pending runs/2026-05-26_17-54-04
```

Or from inside the REPL: `/approve-pending 2026-05-26_17-54-04`.

For each pending it prompts `[a]pprove / [r]eject / [s]kip` and writes a
`pending-NNN.decision.json` sidecar plus appends to `approvals.jsonl`. Idempotent:
re-running skips already-decided items.

---

## Where artifacts go

Every run creates `runs/<YYYY-MM-DD_HH-MM-SS>/`:

```
runs/2026-05-24_11-11-24/
├── transcript.md              # what the model said, what fired, gate decisions
├── 001_initial.png            # screenshot before VSCode hides
├── 002_after_hide_vscode.png
├── 003_open_path.png          # screenshots numbered by step
├── ...
├── final_report.md            # human-readable Gate C output
├── final_report.json          # machine-readable Gate C output
└── review/
    ├── plan.md                # if Gate A ran
    ├── approvals.md           # human-readable Gate B + Gate C log
    ├── approvals.jsonl        # one JSON line per decision
    ├── pending-NNN.json       # if a Gate B prompt failed
    └── pending-NNN.decision.json  # written by --approve-pending
```

`runs/` is in `.gitignore` — don't commit them.

---

## Test coverage

65 tests, ~0.4s on the laptop. No live Anthropic calls; everything mockable.

```bash
.venv/bin/python -m pytest tests/ -q
```

| File | Coverage |
|---|---|
| [tests/test_review.py](../tests/test_review.py) | 34 tests — risk classification, gate policy, file fallback, approvals log, B1 TCC probe, B2 `--approve-pending`, B3 typing heuristic, B4 final-review semantics, final report rendering |
| [tests/test_interactive.py](../tests/test_interactive.py) | 15 tests — REPL command dispatch, quit/EOF, `/help`, `/list`, `/diagnose`, `/approve-pending`, per-task fresh policy |
| [tests/test_native_actions.py](../tests/test_native_actions.py) | 4 tests — `safe_shell` blocklist, `verify_app_installed`, attached-volumes plist parsing |
| [tests/test_context_window.py](../tests/test_context_window.py) | 7 tests — `compact_messages` keeps the N newest image blocks; `mark_rolling_cache` keeps one prompt-cache breakpoint on the latest message |
| [tests/test_tool_result.py](../tests/test_tool_result.py) | 5 tests — errored `tool_result` is forced text-only (API rejects images when `is_error`) |

---

## Known limits & non-goals

- **No web UI.** REPL only. If you want a web interface, that's a separate project.
- **No mid-run resume.** When a run hits a file-fallback `BLOCKED`, the Python process exits. `--approve-pending` records the human decision into the audit log so the final report is complete, but the *same* run does not pick back up. By design (the previous handoff called this out).
- **No multi-task queueing.** REPL runs one task at a time.
- **Spotlight is unreliable.** On the dev host, `cmd+space` is remapped. The system prompt tells Claude to avoid Spotlight; use menu bar / `macos.open_path` instead.
- **Finder drag-to-Applications is flaky.** The model tries one visual drag; if that doesn't clearly start copying, it falls back to `macos.install_app_from_dmg` (verified-working primitive).
- **B5 not done.** The initial screenshot is taken twice (once pre-hide for the planner, once post-hide for the loop). Cheap to fix; deferred to a future session.

---

## How a run looks end-to-end

The verified install proof: [runs/2026-05-24_11-11-24/](../runs/2026-05-24_11-11-24/)

```
Step 2: macos.open_path /Users/apple/Desktop/gc12.dmg
Step 4: macos.hide_vscode
Step 5: computer.left_click_drag [318,200] -> [527,200]   (tried, didn't copy)
Step 6: macos.install_app_from_dmg                          (HIGH — approved)
Step 7: macos.eject_volume GraphicConverter 12
Step 8: macos.verify_app_installed GraphicConverter 12
Final: TASK COMPLETE
```

Machine verification: `/Applications/GraphicConverter 12.app` exists, volume
unmounted. Gate C asked the human, got `pass`.

---

## When something goes wrong

1. **Hit a hard corner of the screen.** `pyautogui.FAILSAFE` is on — any
   corner aborts the action loop.
2. **Ctrl+C in the terminal.** The `finally` in `run_task` still writes
   `final_report.json` so the audit trail isn't lost.
3. **Dialog never showed.** Check the TCC probe output; check
   `runs/<ts>/review/pending-*.json` and run `--approve-pending`.
4. **Got `RequestTooLargeError`.** Lower `--image-history` to 2 or 3.
5. **`MAX_STEPS` hit.** Either the task is too big or the model is stuck.
   Look at `transcript.md` — usually the last 3-4 steps repeat.
