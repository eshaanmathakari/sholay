# Demo & screen-recording runbook

A ~8–10 minute walkthrough that lands the **cost / accuracy / token / time** story across the app
types. `demo.sh` drives the classic flows back-to-back so you can capture one clean take; this doc
is the talk-track and the "what you should see" checklist. Mirrors `docs/ARCHITECTURE.md` §11.
Flow #2 (GitHub PRs + Proton invoices → Notion) is its own take — see the **Flow #2 beat** below.

> **Before you hit record:** read `docs/ARCHITECTURE.md` §13 (Permissions). You need **Screen
> Recording + Accessibility + Automation** granted to your terminal, `ANTHROPIC_API_KEY` in `.env`,
> **Brave** installed, and the **native Proton Mail app** installed *and logged in*.
> For Flow #2 additionally: the **Notion desktop app** logged in with the **"COA test" database**
> — its columns must **already exist** (the agent won't create them): `Name` (title), `Source`
> (select with options *GitHub PR* and *Invoice*), `Status` (select with *Pending*); `Detail`
> (text) is optional. Also: one or more repo changes ready to open as PRs on camera, and a few
> **invoice/billing emails already in the Proton inbox**. No Notion API key is needed — the
> GitHub API verifies the PR reads and the human confirms the Notion rows + invoices.

---

## 0. Setup (off-camera, ~2 min)

```bash
python -u agent.py --diagnose        # preflight: confirms screen/keyboard paths before spending tokens
```

- Run everything from **Terminal.app / iTerm**, **not** the VSCode terminal (the runner hides VSCode).
- Quit Slack/Mail/anything with notifications; full-screen the terminal.
- Have **Brave** and **Proton Mail** open and logged in, then bring the terminal to the front.
- Optional dry run with `./demo.sh --no-pause` on a throwaway take to confirm permissions are live —
  a freshly granted permission often needs the terminal **quit and reopened**.

## 1. Start recording, then run

```bash
./demo.sh
```

It pauses between beats (press **Enter** to advance) so you can narrate. Add `--no-pause` for an
unattended take, or `--tradingview` / `--proton` to record a single flow.

---

## Beat-by-beat (what to say · what you'll see)

**1 — Frame it (30s).** "The old system navigated however it wanted. The ask inverts that: fixed,
written-down flows across three app *types*, each measured for cost, accuracy, tokens — and now time."

**2 — Preflight (15s).** `agent.py --diagnose` runs. "This is the preflight — it checks the screenshot
and mouse/keyboard paths so a failure points at the host, not at a wasted run."

**3 — Flow #1, TradingView, machine-verified (~4 min).** The agent drives **Brave → SPX chart → Daily
→ 4H → reads the close → flags TA patterns from a fixed catalog**.
- *Expected end:* `STATUS: pass   fact_match=True   cost=$0.7x` and an oracle line like
  `close 7511.xx vs quote 7511.xx → 0.00xx%`.
- *Say:* "The oracle used an **independent** market quote — not the chart the agent just looked at."

**4 — The numbers (1 min).** `report.py` regenerates the dashboard; show `docs/results.html`.
- Walk the **cross-app-type table**: pass%, **$/run**, **$/success**, tokens, steps, **time/run**.
- Scroll to **Run-to-run variability**: "Same fixed playbook, but steps and time *drift* run to run.
  TradingView spans **16–52 steps**, **right-skewed** — that one tall outlier is the pre-fix thrash run."

**5 — The efficiency story (1 min).** "First run was **\$2.73 / 52 actions / 502s**. We *measured* the
waste, switched the playbook to URL-parameter navigation, and the same run is now **\$0.73 / 16
actions / 224s**." Determinism, cost, *and* time are things you tune — with data.

**6 — Flow #3, Proton, human-gated (~3 min).** The agent drives the **native Proton Mail app**, selects
the top-5 emails, marks them read.
- The **approval dialog pops** — *say:* "There's no API to verify a mailbox, so the human is the answer
  key." **Click PASS.**
- *Say:* "It still logged real cost, tokens, and time — that's the no_api data point in the comparison."

**7 — Feedback loop (30s).** Show a `feedback` note that overrides a subjective pattern call while the
objective oracle verdict stands — humans tune the YAML by hand; no auto prompt-learning.

**8 — Close (30s).** All three app types live — browser, no-API, and multi-app — each landed as the
same drop-in (a YAML + an oracle), zero framework changes.

---

## Flow #2 beat — GitHub PRs + Proton invoices → one Notion tracker (own take, ~6 min)

The story: **the agent consolidates work items from two systems that don't talk to each other —
a code host and an inbox — into one Notion system-of-record.** That's the "swivel-chair
integration" enterprises pay humans to do all day.

```bash
./demo.sh --github
```

0. **Have ready:** the repo's **open-PRs page** showing in Brave, a few **invoice/billing emails**
   in the Proton inbox, and the **COA test** Notion database (Name / Source / Detail / Status).
1. **Open the PRs on camera** (GitHub web UI, any real changes). *Say:* "These didn't exist ten
   seconds ago — the agent doesn't know their numbers, titles, or authors. Watch it find out."
2. The agent brings **Brave** forward and reads **every open PR** (number / title / author).
3. It switches to the **Notion desktop app → COA test** and adds one row per PR: Name = title,
   Source = *GitHub PR*, Detail = author + #, Status = *Pending*.
4. It switches to **Proton**, searches **invoice / billing**, and reads the matching emails
   (sender + subject). *Say:* "Email has no API — this is pure vision, like a person triaging a
   mailbox."
5. Back in **Notion**, it adds one row per invoice email: Source = *Invoice*.
6. The **approval dialog pops** with the oracle's findings. *Say:* "The GitHub API is the
   independent answer key — it confirmed every PR the agent read is GitHub's real record. Now I
   confirm the Notion rows and that the invoices are genuine billing mail." Check Notion, **click
   PASS**.
- *Expected end:* `STATUS: pass`, reasons listing ✓ per PR (title/author vs GitHub) + the row counts.
- *Recovery:* Notion property fields mis-clicked → FAILSAFE (cursor to a corner), fix/delete the bad
  row, re-run. It's a long two-app flow — **rehearse once off-camera** so the take is the clean one.

---

## Recovery (if something derails on camera)

- **Agent thrashes a step / wrong window focused:** slam the cursor into a screen corner to trip the
  `pyautogui` FAILSAFE, then re-run the single flow (`./demo.sh --tradingview` or `--proton`).
- **Quote source unreachable:** the run records `STATUS: error` (infra noise, *not* an accuracy fail) —
  it's excluded from pass-rate and shown in the `err` column. Re-run; the multi-source quote fetch
  (Yahoo → CNBC → stooq) usually recovers.
- **Proton dialog times out:** that becomes a `needs_review` → `error` row (this is the 555s run you
  see inflating no_api's avg time). Re-run and click **pass** promptly.

The recording is best captured *after* a green dry run, so the on-camera take is the clean one. All
numbers shown in the dashboard come straight from `runs.db` — nothing is hand-entered.
