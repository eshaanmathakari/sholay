"""SQLite store for per-run COA metrics.

One row per run in `runs`, optional per-step rows in `step_metrics`, and human
feedback in `feedback`. Stdlib `sqlite3` only — no new dependency.

There is no best-of-N batching: each agent invocation appends exactly one `runs`
row, and statistics (pass rate, average cost, average tokens) are queries over the
accumulated rows. The detailed per-run artifacts (screenshots, final_report.json)
stay on disk under the path stored in `run_dir`.
"""
import sqlite3
from pathlib import Path
from typing import Union

DEFAULT_DB = Path("runs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  ts             TEXT NOT NULL,
  flow           TEXT NOT NULL,
  app_type       TEXT NOT NULL,
  model          TEXT NOT NULL,
  mode           TEXT NOT NULL,
  status         TEXT NOT NULL,
  steps          INTEGER,
  steps_expected INTEGER,
  retries        INTEGER,
  misclicks      INTEGER,
  in_tok         INTEGER,
  out_tok        INTEGER,
  cache_read     INTEGER,
  cache_write    INTEGER,
  cost_usd       REAL,
  latency_s      REAL,
  fact_match     INTEGER,
  run_dir        TEXT
);

CREATE TABLE IF NOT EXISTS step_metrics (
  run_id      TEXT NOT NULL,
  step_idx    INTEGER NOT NULL,
  goal        TEXT,
  steps       INTEGER,
  retries     INTEGER,
  in_tok      INTEGER,
  out_tok     INTEGER,
  cache_read  INTEGER,
  cache_write INTEGER,
  latency_s   REAL,
  ok          INTEGER,
  PRIMARY KEY (run_id, step_idx)
);

CREATE TABLE IF NOT EXISTS feedback (
  run_id   TEXT NOT NULL,
  ts       TEXT NOT NULL,
  reviewer TEXT,
  verdict  TEXT,
  notes    TEXT,
  PRIMARY KEY (run_id, ts)
);
"""

RUN_COLUMNS = (
    "run_id", "ts", "flow", "app_type", "model", "mode", "status",
    "steps", "steps_expected", "retries", "misclicks",
    "in_tok", "out_tok", "cache_read", "cache_write",
    "cost_usd", "latency_s", "fact_match", "run_dir",
)
STEP_COLUMNS = (
    "run_id", "step_idx", "goal", "steps", "retries",
    "in_tok", "out_tok", "cache_read", "cache_write", "latency_s", "ok",
)
FEEDBACK_COLUMNS = ("run_id", "ts", "reviewer", "verdict", "notes")

_GROUPABLE = {"flow", "app_type", "model"}
_SERIES_METRICS = {"steps", "latency_s", "cost_usd", "retries"}


def connect(db_path: Union[str, Path] = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating tables if needed) and return a connection with Row access."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _insert(conn: sqlite3.Connection, table: str, columns, row: dict) -> None:
    cols = [c for c in columns if c in row]
    if not cols:
        raise ValueError(f"no known columns for {table} in {sorted(row)}")
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )
    conn.commit()


def insert_run(conn: sqlite3.Connection, row: dict) -> None:
    """Append one run. Duplicate `run_id` raises sqlite3.IntegrityError by design —
    every invocation must be a distinct row."""
    _insert(conn, "runs", RUN_COLUMNS, row)


def insert_step(conn: sqlite3.Connection, row: dict) -> None:
    _insert(conn, "step_metrics", STEP_COLUMNS, row)


def insert_feedback(conn: sqlite3.Connection, row: dict) -> None:
    """Append one feedback record linked to a run. Does NOT mutate the run's
    status — an overriding verdict lives here, alongside the oracle's verdict."""
    _insert(conn, "feedback", FEEDBACK_COLUMNS, row)


def aggregate(conn: sqlite3.Connection, group_by: str = "flow") -> list:
    """Per-group rollup: run count, pass %, avg cost, avg total tokens, avg steps, avg time.

    `pass_pct` is computed over *scored* runs only — pass / (pass + fail). An `error`
    run (the oracle couldn't verify, e.g. an unreachable quote source) is infra noise,
    not an accuracy failure (design D3), so it's excluded from the denominator and
    surfaced separately as `errors`. `runs` is still the total of every status; if a
    group has only error runs, `pass_pct` is NULL (renders as "—"), never a false 0%.

    `avg_latency` is the mean wall-clock seconds per run — how long a run *takes*, the
    operational companion to what it costs. Like `avg_cost`, it spans every status.
    """
    if group_by not in _GROUPABLE:
        raise ValueError(f"group_by must be one of {sorted(_GROUPABLE)}")
    cur = conn.execute(
        f"""
        SELECT {group_by} AS grp,
               COUNT(*) AS runs,
               SUM(status IN ('pass', 'fail')) AS scored,
               SUM(status = 'error') AS errors,
               ROUND(100.0 * SUM(status = 'pass')
                     / NULLIF(SUM(status IN ('pass', 'fail')), 0), 1) AS pass_pct,
               ROUND(AVG(cost_usd), 4) AS avg_cost,
               CAST(AVG(COALESCE(in_tok,0) + COALESCE(out_tok,0)
                        + COALESCE(cache_read,0) + COALESCE(cache_write,0)) AS INTEGER) AS avg_tok,
               ROUND(AVG(steps), 1) AS avg_steps,
               ROUND(AVG(latency_s), 1) AS avg_latency
        FROM runs
        GROUP BY {group_by}
        ORDER BY {group_by}
        """
    )
    return [dict(r) for r in cur.fetchall()]


def series(conn: sqlite3.Connection, metric: str = "steps", group_by: str = "flow") -> dict:
    """Per-group list of one metric's raw per-run values, for distribution/skew charts.

    Returns `{group_value: [v, v, ...]}` ordered by run time, so a caller can show how
    much the same flow varies run-to-run (e.g. step counts that drift even on a fixed
    playbook). Only the whitelisted numeric columns are allowed — same SQL-injection
    guard as `aggregate`. NULLs are dropped so the stats downstream stay clean.
    """
    if group_by not in _GROUPABLE:
        raise ValueError(f"group_by must be one of {sorted(_GROUPABLE)}")
    if metric not in _SERIES_METRICS:
        raise ValueError(f"metric must be one of {sorted(_SERIES_METRICS)}")
    cur = conn.execute(
        f"SELECT {group_by} AS grp, {metric} AS val FROM runs "
        f"WHERE {metric} IS NOT NULL ORDER BY {group_by}, ts"
    )
    out: dict = {}
    for r in cur.fetchall():
        out.setdefault(r["grp"], []).append(r["val"])
    return out
