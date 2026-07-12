# Deterministic COA Flows — Plan

**Status:** DRAFT for review · **Owner:** Eshaan · **Date:** 2026-06-16
**Predecessor:** the existing review-gated computer-use agent (`agent.py`, see [NOTES.md](NOTES.md)).

---

## 1. Background & goal

The current system is a *vision-driven autonomous* COA: it screenshots the screen, asks
Claude (`claude-sonnet-4-6`, computer-use) what to do next, gates the action through three
human review gates, and acts via `pyautogui`. The demo's selling point was "it navigates
however it wants."

The manager's new ask **inverts** that: instead of open-ended autonomy, build **three
deterministic flows** across **three different *types* of application**, each run
**monitored for cost, accuracy, and token usage**.

**Goal of this work:** a small harness that runs predefined flows, measures every run, and
accumulates the results so we can answer — per flow and per app-type — *"how reliable is it,
what does a run cost, and how many tokens does it burn?"*

---

## 2. Scope

### In scope
- A **flow runner** that executes a written-down playbook step-by-step via the existing agent.
- A **metrics store** (SQLite) accumulating one row per run + a feedback table.
- A **cost/accuracy/token report** (CLI table + generated HTML).
- **Flow #1 — TradingView (browser):** fully built as the reference implementation.

### Deferred (decide before building #2/#3)
- **Flow #2 — BUILT (2026-07): `github_notion_intake` (multi-app).** The legacy-app slot was
  repurposed for a manager-requested *dynamic, multi-app* workflow: PRs opened live on the repo
  → agent reads every open PR in Brave → logs one Notion row each → reads the invoice/billing
  emails in Proton → logs those too, consolidating two systems into one Notion tracker. Oracle:
  the GitHub API machine-verifies every PR the agent read; the Notion rows + invoice
  classification are human-gated (email has no API; Notion populated by vision, no key needed).
  Legacy app (GraphicConverter 12 was a candidate) is parked indefinitely.
- **Flow #3 — app without a public API:** **BUILT — Proton Mail (native macOS app).** `demo`
  flow: mark the top-5 inbox emails read; human-gate oracle (see §7.2).

### Explicitly out of scope
- No fine-tuning / no automatic prompt-learning (feedback is capture-only; humans tune YAML).
- No best-of-N batch experiments (each invocation is one measured run).
- No changes to the core perception/action primitives in `agent.py` beyond what the runner needs.

---

## 3. Design decisions (locked via review)

| # | Decision | Resolution |
|---|----------|------------|
| D1 | Determinism | **Fixed YAML playbook per flow**; the LLM executes each step live via vision (same plan, live pixels). |
| D2 | Execution model | **Step-by-step, runner-driven** — one sub-goal at a time; agent signals `STEP DONE`; runner records per-step tokens/time/retries before advancing. History carries across steps via the existing rolling cache. |
| D3 | Accuracy | **Layered:** (a) success rate via a per-flow oracle, (b) efficiency = steps/retries/misclicks vs expected, (c) output correctness via a checkable emitted fact. |
| D4 | Sampling | **No best-of-N.** Each run = one row. Success rate / averages are *queries over accumulated history*. |
| D5 | Storage | **SQLite `runs.db`** (one row/run) + `feedback` table. Existing `final_report.json` stays as the detailed per-run artifact. |
| D6 | Execution mode | **Fully autonomous** (review gates off) for clean agent-only metrics. These are reviewed demo/showcase flows; safety kept light. |
| D7 | Feedback loop | **Capture-only.** Post-run human verdict (can override oracle) + free-text notes → `feedback` table. Humans tune the YAML by hand. No auto prompt-injection. |
| D8 | Cost | `tokens × verified Sonnet-4.6 price` (see §6). No fabricated numbers. |
| D9 | Reporting | One query layer → CLI markdown table (dev) + generated `docs/results.html` (boss), styled like `brief.html`, with a cross-app-type comparison table. |
| D10 | Build sequence | Framework end-to-end on Flow #1 first; #2/#3 become drop-in specs once apps are chosen. |

---

## 4. Components

| Component | File (proposed) | Responsibility |
|-----------|-----------------|----------------|
| Flow spec | `flows/tradingview.yaml` | Ordered steps + oracle config + metadata (app_type, model, mode). |
| Spec loader | `flows/loader.py` | Parse + validate a spec (fail fast on a malformed playbook). |
| Runner | `runner.py` | Drive the spec step-by-step through the agent loop; collect per-step + total metrics; run the oracle; write a `runs.db` row + `final_report.json`. |
| Oracles | `oracles/` | One verifier per flow. `oracles/tradingview.py` = close-price-vs-quote + structural checks. |
| Metrics DB | `metrics_db.py` | `runs` + `feedback` tables; insert helpers; aggregate queries. Stdlib `sqlite3`, zero new deps. |
| Pricing | `pricing.py` | Verified per-model `$/MTok` table; `cost_usd(usage, model)`. |
| Feedback CLI | `feedback.py` | `python feedback.py <run_id>` → capture verdict + notes into `feedback`. |
| Report | `report.py` | Query `runs.db` → print CLI table **and** render `docs/results.html`. |

New runtime dependency: **`PyYAML`** (spec parsing). Everything else is stdlib or already present.

---

## 5. Data model (`runs.db`)

```sql
CREATE TABLE runs (
  run_id        TEXT PRIMARY KEY,   -- timestamp-based, matches the run artifact dir
  ts            TEXT NOT NULL,      -- ISO 8601 UTC
  flow          TEXT NOT NULL,      -- e.g. "tradingview_spx"
  app_type      TEXT NOT NULL,      -- "browser" | "legacy" | "no_api"
  model         TEXT NOT NULL,
  mode          TEXT NOT NULL,      -- "measure" | "demo"
  status        TEXT NOT NULL,      -- "pass" | "fail" | "error"
  steps         INTEGER,            -- steps actually executed
  steps_expected INTEGER,           -- from the spec
  retries       INTEGER,            -- step re-attempts
  misclicks     INTEGER,            -- detected wrong-target clicks (best-effort)
  in_tok        INTEGER,
  out_tok       INTEGER,
  cache_read    INTEGER,
  cache_write   INTEGER,
  cost_usd      REAL,
  latency_s     REAL,
  fact_match    INTEGER,            -- 1/0/NULL: emitted fact matched ground truth
  run_dir       TEXT                -- path to artifacts (screenshots, final_report.json)
);

CREATE TABLE step_metrics (        -- optional granular layer (D2/D3)
  run_id TEXT, step_idx INTEGER, goal TEXT,
  steps INTEGER, retries INTEGER, in_tok INTEGER, out_tok INTEGER,
  cache_read INTEGER, cache_write INTEGER, latency_s REAL, ok INTEGER,
  PRIMARY KEY (run_id, step_idx)
);

CREATE TABLE feedback (
  run_id TEXT, ts TEXT, reviewer TEXT,
  verdict TEXT,            -- "pass" | "fail" (may override oracle)
  notes TEXT,
  PRIMARY KEY (run_id, ts)
);
```

Example report query:
```sql
SELECT flow, app_type, COUNT(*) AS runs,
       ROUND(100.0*SUM(status='pass')/COUNT(*),1) AS pass_pct,
       ROUND(AVG(cost_usd),4) AS avg_cost,
       CAST(AVG(in_tok+out_tok+cache_read+cache_write) AS INT) AS avg_tok,
       ROUND(AVG(steps),1) AS avg_steps
FROM runs GROUP BY flow;
```

---

## 6. Cost model (verified)

Default model **`claude-sonnet-4-6`**, from the Claude API pricing reference.
`claude-sonnet-5` is also priced in `pricing.py` (introductory rates through
2026-08-31) so switching the default is a one-line change that reprices correctly:

| Token class | sonnet-4-6 (default) $ / MTok | sonnet-5 (option, intro) $ / MTok |
|---|---|---|
| Input (uncached) | 3.00 | 2.00 |
| Output | 15.00 | 10.00 |
| Cache write (5-min TTL) | 3.75 | 2.50 |
| Cache read | 0.30 | 0.20 |

```
cost_usd = ( in_tok*3.00 + out_tok*15.00 + cache_write*3.75 + cache_read*0.30 ) / 1e6
```

Image/screenshot tokens are **already inside** the API-reported `input_tokens` / cache counts,
so no image-token estimation is needed. `pricing.py` holds the table so a model swap is one edit.
A derived **`$/successful run` = avg_cost ÷ pass_rate** is the headline efficiency metric.

---

## 7. Flow #1 — TradingView (browser)

**Playbook (`flows/tradingview.yaml`):**
1. Open the browser (Brave).
2. Go to `tradingview.com`.
3. Open the S&P 500 (SPX) chart.
4. Set timeframe = Daily; screenshot.
5. Set timeframe = 4H; screenshot.
6. Read the last close; **flag technical-analysis patterns visible on the chart** using a
   bundled open-source pattern reference (`flows/ta_patterns.json` — names + definitions of
   standard chart patterns: double top/bottom, head & shoulders, triangles, flags, wedges,
   channels, etc.); write a paragraph of observations; emit
   `{ last_close, tf_daily, tf_4h, patterns: [{name, timeframe, rationale}], paragraph }`.

The pattern reference is injected into the flow's prompt so the agent **names patterns from a
fixed catalog** (consistent, checkable vocabulary) rather than inventing labels.

**Oracle (`oracles/tradingview.py`):** PASS iff
- `|last_close − independent_quote| / independent_quote ≤ 0.005` (±0.5%), AND
- both timeframes were covered, AND
- ≥1 flagged pattern's `name` is in the known catalog (structural validity), AND
- paragraph is non-empty.

Independent quote source: **stooq** (`^spx`, no API key) with a fallback; tolerance absorbs
after-hours / cross-source drift. **Whether a flagged pattern is *genuinely* present is NOT in
the oracle** (that's subjective) — pattern correctness and prose quality go to the human
feedback step (D7). The oracle only checks the *objective fact* (close price) and *structural
validity* (patterns named from the catalog, both timeframes covered).

## 7.2 Flow #3 — Proton Mail (no public API) — BUILT (`demo` flow)

**App type:** `no_api`, run against the **native Proton Mail macOS app** (not the web app in a
browser — a browser surface would just repeat Flow #1's app type and defeat the three-app-type
contrast). Files: `flows/proton.yaml` + `oracles/proton.py`.

**Task (demo):** mark the **top 5 inbox emails read**. The agent activates the app (macos
`activate_app`), selects the 5 top rows one by one, clicks "Mark as read", and emits
`{ marked_read: [{sender, subject}×5], unread_before, unread_after }`. Spec runs as `mode: demo`.

**Repeatability — resolved (this was the open §11 decision).** We dropped the precision/recall
measurement design. Marking read is **reversible** (unlike deletion) and low-stakes, and this
flow is a **showcase, not a measured-accuracy flow** — ~2 runs (a shakeout + the live demo), no
labeled ground-truth set, no ≥5-run statistics.

**Oracle (`oracles/proton.py`) — the human IS the oracle.** With no API to verify mailbox state
against, `run_oracle` pops the legacy Gate-C review dialog (`review.request_final_review`)
summarizing what the agent did; the human's click becomes the run status — **Approve → `pass`,
No → `fail`, Feedback/timeout → `needs_review` → `error`**. `fact_match` is always `None` (no
machine-checkable fact). Tests inject a fake reviewer, so no dialog pops in CI.

This is the measure/demo split (D6/D7) taken to its end: Flow #1 is machine-verified (independent
quote); a no-API flow is **human-verified by design** — which also revives the human-in-the-loop
safety story the autonomous browser flow turns off. The run still records real cost/tokens/steps,
so the manager's cross-app-type **cost/token** comparison still gets its `no_api` data point.

---

## 8. Testing conditions

Each component must pass these before it's considered done.

**T1 — Spec loader**
- Valid spec parses into the expected structure.
- Malformed spec (missing `steps`, unknown `app_type`, bad oracle config) fails fast with a
  clear error — does **not** silently run.

**T2 — Pricing**
- `cost_usd` on a known fixed `usage` dict equals the hand-computed value (unit test with the §6 rates).
- A run with cache reads costs strictly less than the same run priced as all-uncached.

**T3 — Metrics DB**
- Inserting a run row then querying it returns identical values (round-trip).
- Aggregate query returns correct pass-rate / averages on a seeded 3-row fixture.
- Two runs of the same flow append two rows (no overwrite); `run_id` is unique.

**T4 — Oracle (TradingView)**
- Synthetic emit within tolerance → PASS; outside tolerance → FAIL.
- Missing timeframe or empty paragraph → FAIL.
- Quote-source unreachable → oracle returns a distinct `error` status (not a false PASS/FAIL).

**T5 — Runner (integration, live)**
- One real autonomous run of Flow #1 completes end-to-end, writes exactly one `runs.db` row
  **and** a `final_report.json`, and the row's token/cost fields are populated and non-zero.
- A deliberately broken step (e.g. wrong URL) yields `status='fail'` or `'error'`, not `'pass'`.
- Re-running produces a second independent row with no side effects on the first.

**T6 — Feedback**
- `feedback.py <run_id>` writes a linked `feedback` row; an overriding verdict is recorded
  without mutating the original `runs.status`.

**T7 — Report**
- CLI table and `results.html` are generated from the same query and show identical numbers.
- With ≥2 flows present, the cross-app-type comparison table renders all of them.
- Empty DB → report renders gracefully ("no runs yet"), no crash.

---

## 9. Exit criteria (definition of done)

**Phase 1 — Framework + Flow #1 (this milestone):**
1. T1–T7 all pass.
2. Flow #1 has been run autonomously **≥5 times**; `runs.db` holds those rows with real
   cost/token/accuracy values (real measured numbers, not placeholders).
3. `report.py` produces a CLI table and `docs/results.html` showing Flow #1's pass-rate,
   avg cost, avg tokens, avg steps, and `$/successful run`.
4. A human has exercised the feedback path on ≥1 run.
5. Adding a new flow is documented as "drop a YAML in `flows/` + an oracle in `oracles/`" —
   no framework code changes required.

**Phase 2 — Flows #2 & #3:**
6. Each has a YAML spec + an oracle and passes its own T4/T5 equivalents. **Flow #3 (Proton) is
   a `demo` flow** — human-gated, ~2 runs, *not* a ≥5-run measured-accuracy flow (see §7.2).
   **Flow #2 (github_pr, multi-app) is also a `demo` flow** — its input (a live PR) is created
   fresh per run, so repeat-run accuracy stats apply per-demo rather than as a ≥5-run series.
7. `results.html` shows the cross-app-type comparison the manager asked for — **browser + no_api
   live**; the legacy column lands when app #2 is chosen.

---

## 10. Milestones / sequence

1. ✅ `pricing.py` + `metrics_db.py` + tests (T2, T3) — **done, 10 tests green.**
2. ✅ `flows/loader.py` + `flows/tradingview.yaml` + `flows/ta_patterns.json` + `oracles/tradingview.py` + tests (T1, T4) — **done, 19 tests green.**
3. ✅ `runner.py` (reuses agent.py primitives, step-by-step) — **code done & compiles.** First live run (T5) is the user's to trigger.
4. ✅ `feedback.py` (T6) + `report.py` → `docs/results.html` (T7) — **done, 7 tests green; report verified end-to-end.**
5. ⏳ Run Flow #1 ≥5× (live, on the Mac); review numbers; hit Phase-1 exit criteria. **Blocked on the live run only.**
6. ✅ Proton #3 built as a native-app `demo` flow (mark top-5 read, human-gate oracle); ran green live.
7. ✅ Flow #2 built as `github_notion_intake` (multi-app `demo` flow: open PRs + Proton invoice
   emails → one Notion tracker; GitHub-API machine oracle + human gate). Legacy app parked.

> Build status: 64/64 offline tests pass. Only the live runs (T5 / §9 criterion 2) remain for Phase 1 — they need the Mac + an API key and can't be run autonomously here. Open TODOs noted in code: `demo`-mode gates aren't wired into the runner yet (use `agent.py` for the gated demo); `misclicks` is a best-effort proxy (counts step retries).

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| TradingView UI varies (login walls, popups, layout shifts) → flaky runs | Oracle tolerance + retries; capture screenshots every step for diagnosis; treat unreachable quote source as `error`, not `fail`, so infra noise doesn't pollute accuracy. |
| "Misclick" detection is fuzzy | Best-effort metric (count step re-attempts); document it as indicative, not exact. |
| Repeated runs of #3 (Proton Mail) mutate real mail | **Resolved:** #3 is a `demo` flow that **marks read** (reversible, low-stakes), not deletes; ~2 runs, human-gated, not measured for accuracy (§7.2). No reset apparatus or seeded account needed. |
| TA pattern flags can't be objectively verified | Oracle checks only *structural validity* (named from the catalog) + the objective close price; genuine-pattern-presence and prose go to human feedback (D7). |
| Autonomous mode removes the safety-gate story for these flows | Keep a `demo` mode that re-enables gates for live showcases (D6). |
| Cost/accuracy numbers misread as best-of-N | Report wording makes clear these are cumulative per-run stats over history (D4). |

---

## 12. Sign-off status
- ✅ Framework design (§3) and SQLite schema (§5) — **approved**.
- ✅ Add **PyYAML** as the one new dependency — **approved**.
- ✅ Browser = **Brave** for Flow #1 — **approved**.
- ✅ No-API app (#3) — **Proton Mail, BUILT** as a native-app `demo` flow: mark top-5 read,
  human-gate oracle (§7.2). Ran green live (`pass`).
- ⏳ Legacy app (#2) — still being chosen; Phase 2 blocked on it.
- 🆕 Flow #1 enhanced to **flag TA patterns** against a bundled catalog (§7).
