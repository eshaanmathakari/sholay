"""Report COA metrics from runs.db — CLI table + a generated HTML page.

One data-gathering pass (`gather`) feeds two renderers (`render_cli`, `render_html`)
so the terminal and the boss-facing page always show identical numbers. The headline
business metric is `$ / successful run` = avg_cost ÷ pass_rate.

Usage:
    python report.py                 # print table + write docs/results.html
    python report.py --no-html       # table only
"""
import argparse
import html
import sys
from datetime import datetime

import metrics_db
import pricing

DEFAULT_HTML_OUT = "docs/results.html"


def _cost_per_success(avg_cost, pass_pct):
    if not avg_cost or not pass_pct:
        return None
    return round(avg_cost / (pass_pct / 100.0), 4)


def _enrich(rows):
    for r in rows:
        r["cost_per_success"] = _cost_per_success(r.get("avg_cost"), r.get("pass_pct"))
    return rows


def _stats(values) -> dict:
    """Summary stats for one flow's per-run values (steps, latency, …).

    Reports min / median / mean / max plus a skew read. Skew uses Pearson's second
    coefficient — 3·(mean − median)/stdev — which is robust on the small samples these
    flows produce; it's only reported with ≥3 runs and some spread, otherwise None.
    A positive value means a right tail (a few slow/expensive runs drag the mean up).
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "values": []}
    s = sorted(vals)
    lo, hi = s[0], s[-1]
    mean = sum(s) / n
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    skew = None
    if n >= 3 and hi > lo:
        sd = (sum((v - mean) ** 2 for v in s) / n) ** 0.5
        skew = 0.0 if sd == 0 else round(3 * (mean - median) / sd, 2)
    return {
        "n": n, "values": s, "min": round(lo, 1), "max": round(hi, 1),
        "mean": round(mean, 1), "median": round(median, 1),
        "spread": round(hi - lo, 1), "skew": skew,
    }


def _skew_label(st: dict) -> str:
    if st.get("n", 0) < 3:
        return "too few runs to read skew"
    sk = st.get("skew")
    if sk is None:
        return "flat (no spread)"
    if sk > 0.2:
        return f"right-skewed (+{sk}) — a few long runs pull the mean up"
    if sk < -0.2:
        return f"left-skewed ({sk})"
    return f"~symmetric ({sk})"


def _variability(conn) -> list:
    """Per-flow distribution of step count and wall-clock time, for the skew charts."""
    steps = metrics_db.series(conn, metric="steps", group_by="flow")
    latency = metrics_db.series(conn, metric="latency_s", group_by="flow")
    out = []
    for grp in steps:  # series() already orders by flow
        out.append({
            "grp": grp,
            "steps": _stats(steps[grp]),
            "latency": _stats(latency.get(grp, [])),
        })
    return out


def gather(conn, recent_limit: int = 20, generated: str = None) -> dict:
    by_flow = _enrich(metrics_db.aggregate(conn, group_by="flow"))
    by_app_type = _enrich(metrics_db.aggregate(conn, group_by="app_type"))
    cur = conn.execute(
        "SELECT run_id, ts, flow, app_type, status, cost_usd, "
        "(COALESCE(in_tok,0)+COALESCE(out_tok,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0)) AS tokens, "
        "steps, latency_s, fact_match FROM runs ORDER BY ts DESC LIMIT ?",
        (recent_limit,),
    )
    recent = [dict(r) for r in cur.fetchall()]
    tot = conn.execute(
        "SELECT COUNT(*) AS runs, SUM(status='pass') AS passes, SUM(status='error') AS errors, "
        "ROUND(SUM(cost_usd),4) AS cost, "
        "SUM(COALESCE(in_tok,0)+COALESCE(out_tok,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0)) AS tokens "
        "FROM runs"
    ).fetchone()
    totals = {
        "runs": tot["runs"], "passes": tot["passes"] or 0, "errors": tot["errors"] or 0,
        "cost": tot["cost"], "tokens": tot["tokens"] or 0,
        "flows": len(by_flow), "app_types": len(by_app_type),
    }
    return {"by_flow": by_flow, "by_app_type": by_app_type, "recent": recent,
            "totals": totals, "variability": _variability(conn), "generated": generated}


# ---- rendering ----------------------------------------------------------------

def _fmt(v, money=False):
    if v is None:
        return "—"
    if money:
        return f"${v:.4f}"
    return str(v)


def _secs(v):
    """Wall-clock seconds for a run, e.g. 224.3 -> '224.3s'."""
    return "—" if v is None else f"{v:.1f}s"


def _cli_table(title, rows, key="grp"):
    out = [f"\n{title}"]
    if not rows:
        out.append("  (no runs yet)")
        return "\n".join(out)
    header = (f"  {key:<16} {'runs':>5} {'err':>4} {'pass%':>6} {'$/run':>9} "
              f"{'$/success':>10} {'tokens':>8} {'steps':>6} {'time/run':>9}")
    out.append(header)
    out.append("  " + "-" * (len(header) - 2))
    for r in rows:
        out.append(
            f"  {str(r[key]):<16} {r['runs']:>5} {_fmt(r.get('errors')):>4} {_fmt(r['pass_pct']):>6} "
            f"{_fmt(r['avg_cost'], money=True):>9} {_fmt(r['cost_per_success'], money=True):>10} "
            f"{_fmt(r['avg_tok']):>8} {_fmt(r['avg_steps']):>6} {_secs(r.get('avg_latency')):>9}"
        )
    return "\n".join(out)


def _cli_variability(rows) -> str:
    """Per-flow spread of steps and time-per-run — same numbers the SVG charts show."""
    out = ["\nRun-to-run variability (same playbook, different paths)"]
    if not rows:
        out.append("  (no runs yet)")
        return "\n".join(out)
    for r in rows:
        st, lt = r["steps"], r["latency"]
        out.append(f"  {str(r['grp']):<18} steps: median {_fmt(st.get('median'))} · "
                   f"mean {_fmt(st.get('mean'))} · range {_fmt(st.get('min'))}–{_fmt(st.get('max'))} "
                   f"· {_skew_label(st)}")
        out.append(f"  {'':<18} time : median {_secs(lt.get('median'))} · "
                   f"mean {_secs(lt.get('mean'))} · range {_secs(lt.get('min'))}–{_secs(lt.get('max'))}")
    return "\n".join(out)


def render_cli(data: dict) -> str:
    parts = ["COA flow metrics"]
    if not data["recent"]:
        parts.append("\n(no runs recorded yet)")
        return "\n".join(parts)
    parts.append(_cli_table("By flow", data["by_flow"], key="grp"))
    parts.append(_cli_table("By app type", data["by_app_type"], key="grp"))
    parts.append(_cli_variability(data.get("variability", [])))
    return "\n".join(parts)


_CSS = """
:root { --bg:#ffffff; --fg:#1a1a1a; --muted:#6b7280; --line:#e5e7eb; --card:#f9fafb; --accent:#b4532a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e8e8e8; --muted:#9aa0aa; --line:#262b33; --card:#161a21; --accent:#e08a5c; }
}
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; margin:0; padding:2.5rem; }
h1 { font-size:1.6rem; margin:0 0 .25rem; }
.sub { color:var(--muted); margin:0 0 2rem; font-size:.9rem; }
h2 { font-size:1.1rem; margin:2rem 0 .75rem; }
.cards { display:flex; flex-wrap:wrap; gap:1rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:1rem 1.25rem; min-width:200px; }
.card .flow { font-weight:600; }
.card .big { font-size:1.8rem; font-weight:700; }
.card .row { color:var(--muted); font-size:.85rem; display:flex; justify-content:space-between; gap:1rem; }
table { border-collapse:collapse; width:100%; margin-top:.5rem; font-variant-numeric:tabular-nums; }
th,td { text-align:right; padding:.5rem .75rem; border-bottom:1px solid var(--line); }
th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:600; font-size:.8rem; text-transform:uppercase; letter-spacing:.03em; }
.pass { color:#16a34a; } .fail { color:#dc2626; } .error { color:var(--accent); }
.empty { color:var(--muted); padding:2rem 0; }
.lede { font-size:1rem; max-width:62rem; margin:0 0 1.4rem; }
.totals { display:flex; flex-wrap:wrap; gap:1rem; margin:0 0 1rem; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.7rem 1.1rem; min-width:118px; }
.stat .n { font-size:1.5rem; font-weight:700; }
.stat .l { color:var(--muted); font-size:.76rem; text-transform:uppercase; letter-spacing:.03em; }
.prose { max-width:62rem; } .prose p { margin:.5rem 0; } .muted { color:var(--muted); }
.pipe { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; margin:.6rem 0 1rem; }
.pipe .box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:.5rem .7rem; font-size:.82rem; font-weight:600; text-align:center; }
.pipe .box small { display:block; font-weight:400; color:var(--muted); font-size:.72rem; margin-top:.15rem; }
.pipe .arr { color:var(--accent); font-weight:700; }
footer { color:var(--muted); font-size:.82rem; margin-top:2.5rem; border-top:1px solid var(--line); padding-top:1rem; }
nav.sitenav { display:flex; flex-wrap:wrap; align-items:center; gap:.55rem; font-size:.85rem; margin:0 0 1.6rem; }
nav.sitenav .brand { font-weight:600; color:var(--fg); }
nav.sitenav a { color:var(--muted); text-decoration:none; padding:.25rem .55rem; border-radius:6px; }
nav.sitenav a:hover { color:var(--fg); background:var(--card); }
nav.sitenav .cur { color:var(--fg); background:var(--card); border:1px solid var(--line); padding:.2rem .5rem; border-radius:6px; }
nav.sitenav .sep { color:var(--line); }
code { background:var(--card); border:1px solid var(--line); border-radius:5px; padding:.05rem .3rem; font-size:.85em; }
.dists { display:flex; flex-direction:column; gap:.4rem; }
.distrow { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.9rem 1.1rem; }
.distrow .flow { font-weight:600; } .distrow .metric { color:var(--muted); font-size:.8rem; margin-left:.5rem; }
.distrow .diststat { color:var(--muted); font-size:.8rem; margin-top:.15rem; }
.distgrid { display:grid; grid-template-columns:1fr 1fr; gap:1rem 1.5rem; align-items:end; margin-top:.5rem; }
@media (max-width:640px) { .distgrid { grid-template-columns:1fr; } }
svg.dist { width:100%; max-width:480px; height:auto; overflow:visible; font:11px ui-monospace,SFMono-Regular,monospace; }
.dist .ax { stroke:var(--line); stroke-width:1.5; }
.dist .pt { fill:var(--accent); fill-opacity:.85; }
.dist .med { stroke:var(--fg); stroke-width:2; }
.dist .mean { stroke:var(--muted); stroke-width:1.5; stroke-dasharray:3 2; }
.dist .end, .dist .mlbl { fill:var(--muted); }
.dist .med-t { fill:var(--fg); }
"""


def _h(v, money=False):
    return html.escape(_fmt(v, money=money))


def _svg_strip(st: dict, *, width: int = 460, height: int = 78) -> str:
    """Inline SVG dot/strip plot of one flow's per-run values.

    Each run is a dot (ties stack upward); the solid tick is the median, the dashed
    tick the mean — the gap between them *is* the skew. No JS, no chart library, theme
    colours via the page's CSS variables, so it renders offline and prints cleanly.
    """
    n = st.get("n", 0)
    if n == 0:
        return '<svg class="dist" viewBox="0 0 460 24"><text x="0" y="16" class="end">no runs</text></svg>'
    lo, hi = st["min"], st["max"]
    pad = max(1.0, (hi - lo) * 0.10) if hi > lo else 1.0
    a0, a1 = lo - pad, hi + pad
    x0, x1 = 18.0, width - 18.0
    base = height - 22
    span = (a1 - a0) or 1.0
    X = lambda v: x0 + (x1 - x0) * ((v - a0) / span)

    parts = [f'<svg class="dist" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<line class="ax" x1="{x0:.1f}" y1="{base}" x2="{x1:.1f}" y2="{base}"/>')
    stacks: dict = {}
    for v in st["values"]:
        k = round(v, 3)
        idx = stacks.get(k, 0)
        stacks[k] = idx + 1
        parts.append(f'<circle class="pt" cx="{X(v):.1f}" cy="{base - 6 - idx * 9:.1f}" r="3.4"/>')
    mx = X(st["median"])
    parts.append(f'<line class="med" x1="{mx:.1f}" y1="{base - 42:.1f}" x2="{mx:.1f}" y2="{base + 6:.1f}"/>')
    parts.append(f'<text class="med-t" x="{mx:.1f}" y="{base - 46:.1f}" text-anchor="middle">med {st["median"]:g}</text>')
    if abs(st["mean"] - st["median"]) > 1e-9:
        mnx = X(st["mean"])
        anchor = "start" if mnx >= mx else "end"
        parts.append(f'<line class="mean" x1="{mnx:.1f}" y1="{base - 28:.1f}" x2="{mnx:.1f}" y2="{base + 6:.1f}"/>')
        parts.append(f'<text class="mlbl" x="{mnx + (4 if anchor == "start" else -4):.1f}" y="{base - 30:.1f}" text-anchor="{anchor}">mean {st["mean"]:g}</text>')
    parts.append(f'<text class="end" x="{x0:.1f}" y="{base + 16:.1f}" text-anchor="start">{lo:g}</text>')
    parts.append(f'<text class="end" x="{x1:.1f}" y="{base + 16:.1f}" text-anchor="end">{hi:g}</text>')
    parts.append('</svg>')
    return "".join(parts)


def _render_variability(rows) -> str:
    """The 'Run-to-run variability' section: a steps strip + a time strip per flow."""
    if not rows:
        return ""
    blocks = []
    for r in rows:
        st, lt = r["steps"], r["latency"]
        flow = html.escape(str(r["grp"]))
        steps_stat = (f'{st.get("n", 0)} runs · median {_h(st.get("median"))} · mean {_h(st.get("mean"))} · '
                      f'range {_h(st.get("min"))}–{_h(st.get("max"))} · {html.escape(_skew_label(st))}')
        time_stat = (f'median {html.escape(_secs(lt.get("median")))} · mean {html.escape(_secs(lt.get("mean")))} · '
                     f'range {html.escape(_secs(lt.get("min")))}–{html.escape(_secs(lt.get("max")))}')
        blocks.append(
            f'<div class="distrow"><div><span class="flow">{flow}</span></div>'
            f'<div class="distgrid">'
            f'<div><div class="metric">steps / run</div>{_svg_strip(st)}'
            f'<div class="diststat">{steps_stat}</div></div>'
            f'<div><div class="metric">time / run (seconds)</div>{_svg_strip(lt)}'
            f'<div class="diststat">{time_stat}</div></div>'
            f'</div></div>'
        )
    return (
        '<h2>Run-to-run variability</h2>'
        '<div class="prose"><p>The playbook is fixed, but the agent never walks the exact same path twice — '
        'the screen shifts, the model picks differently, so <strong>step count and wall-clock time drift run to run</strong>. '
        'Each dot is one run; the solid tick is the median, the dashed tick the mean. '
        'When the mean sits to the right of the median the flow is <em>right-skewed</em> — a few long runs '
        '(the pre-fix TradingView thrash, a Proton dialog that waited on a human) drag the average up.</p></div>'
        f'<div class="dists">{"".join(blocks)}</div>'
    )


def _html_rows(rows, key="grp"):
    if not rows:
        return '<tr><td colspan="9" class="empty">no runs yet</td></tr>'
    out = []
    for r in rows:
        out.append(
            f"<tr><td>{html.escape(str(r[key]))}</td><td>{r['runs']}</td>"
            f"<td>{_h(r.get('errors'))}</td>"
            f"<td>{_h(r['pass_pct'])}</td><td>{_h(r['avg_cost'], money=True)}</td>"
            f"<td>{_h(r['cost_per_success'], money=True)}</td><td>{_h(r['avg_tok'])}</td>"
            f"<td>{_h(r['avg_steps'])}</td><td>{html.escape(_secs(r.get('avg_latency')))}</td></tr>"
        )
    return "\n".join(out)


def _card(r):
    """One headline card per flow: pass% over scored runs, with infra-error count when present."""
    pp = f'{_h(r["pass_pct"])}%' if r.get("pass_pct") is not None else "—"
    err = r.get("errors") or 0
    extra = (f'<div class="row"><span>{r.get("scored", 0)} scored</span>'
             f'<span>{err} infra err</span></div>') if err else ""
    return (
        f'<div class="card"><div class="flow">{html.escape(str(r["grp"]))}</div>'
        f'<div class="big">{pp}</div>'
        f'<div class="row"><span>{r["runs"]} runs</span><span>{_h(r["avg_cost"], money=True)}/run</span></div>'
        f'<div class="row"><span>{_h(r["cost_per_success"], money=True)}/success</span>'
        f'<span>{_h(r["avg_tok"])} tok</span></div>'
        f'<div class="row"><span>{html.escape(_secs(r.get("avg_latency")))}/run</span>'
        f'<span>{_h(r["avg_steps"])} steps</span></div>'
        f'{extra}</div>'
    )


def render_html(data: dict) -> str:
    gen = data.get("generated") or ""
    if not data["recent"]:
        body = '<p class="empty">No runs recorded yet. Run a flow with <code>runner.py</code>.</p>'
    else:
        cards = "".join(_card(r) for r in data["by_flow"])
        recent = "".join(
            f'<tr><td>{html.escape(r["flow"])}</td><td>{html.escape(r["app_type"])}</td>'
            f'<td class="{html.escape(r["status"])}">{html.escape(r["status"])}</td>'
            f'<td>{_h(r["cost_usd"], money=True)}</td><td>{_h(r["tokens"])}</td>'
            f'<td>{_h(r["steps"])}</td><td>{_h(r["latency_s"])}s</td>'
            f'<td>{html.escape(str(r["ts"]))}</td></tr>'
            for r in data["recent"]
        )
        t = data.get("totals", {})
        totals_html = (
            '<div class="totals">'
            f'<div class="stat"><div class="n">{t.get("runs", 0)}</div><div class="l">runs</div></div>'
            f'<div class="stat"><div class="n">{_h(t.get("cost"), money=True)}</div><div class="l">total spend</div></div>'
            f'<div class="stat"><div class="n">{t.get("tokens", 0):,}</div><div class="l">tokens</div></div>'
            f'<div class="stat"><div class="n">{t.get("flows", 0)}</div><div class="l">flows</div></div>'
            f'<div class="stat"><div class="n">{t.get("app_types", 0)}</div><div class="l">app types</div></div>'
            '</div>'
        )
        architecture = """
        <h2>How it works</h2>
        <div class="pipe">
          <div class="box">YAML playbook<small>flows/*.yaml</small></div><div class="arr">&rarr;</div>
          <div class="box">Runner<small>step-by-step</small></div><div class="arr">&rarr;</div>
          <div class="box">Claude computer-use<small>screenshot &rarr; action</small></div><div class="arr">&rarr;</div>
          <div class="box">Oracle<small>machine or human</small></div><div class="arr">&rarr;</div>
          <div class="box">runs.db<small>cost &middot; tokens &middot; status</small></div><div class="arr">&rarr;</div>
          <div class="box">This report</div>
        </div>
        <div class="prose">
        <p>Each flow is a fixed, written-down playbook the agent executes step-by-step by looking at
        the screen &mdash; no scripted clicks. Every run is scored by a per-flow <strong>oracle</strong>
        and recorded as one row: cost, tokens, accuracy.</p>
        <p><strong>The oracle is the independent answer key</strong>, chosen to fit the app type.
        <strong>Machine-verified</strong> where a source of truth exists &mdash; the browser flow checks
        the price the agent read off the chart against an independent market quote (&plusmn;0.5%).
        <strong>Human-verified</strong> where none does &mdash; the no-API mail flow pops an approval
        dialog and the reviewer's click is the verdict. Both still record the same cost/token metrics.</p>
        </div>"""
        # Render the $/MTok table straight from pricing.PRICES so the dashboard
        # can never drift from the rates actually used to compute cost_usd.
        _rate_labels = [
            ("input_tokens", "input (uncached)"),
            ("output_tokens", "output"),
            ("cache_creation_input_tokens", "cache write (5-min)"),
            ("cache_read_input_tokens", "cache read"),
        ]
        _models = list(pricing.PRICES)
        _head = "".join(f"<th>{html.escape(m)}</th>" for m in _models)
        _rows = "".join(
            "<tr><td>" + label + "</td>"
            + "".join(f"<td>{pricing.PRICES[m][field]:.2f}</td>" for m in _models)
            + "</tr>"
            for field, label in _rate_labels
        )
        cost_usage = f"""
        <h2>Cost &amp; usage</h2>
        <div class="prose"><p>Cost comes from the API-reported token counts at verified
        per-model rates (from <code>pricing.py</code>; the default is
        <code>claude-sonnet-4-6</code>, with <code>claude-sonnet-5</code> also priced at
        introductory rates through 2026-08-31); screenshot/image tokens are already inside
        these counts.</p></div>
        <table style="max-width:38rem"><thead><tr><th>token class ($/MTok)</th>{_head}</tr></thead>
        <tbody>
        {_rows}
        </tbody></table>"""
        cost_usage += """
        <div class="prose"><p class="muted"><strong>How to read the tables:</strong> <em>pass%</em> is
        over <em>scored</em> runs only (pass &divide; (pass + fail)). An <code>error</code> means the
        oracle <em>couldn't verify</em> (unreachable source, dialog timeout) &mdash; shown separately as
        <em>err</em>, never counted as an accuracy failure. <em>$/success</em> = avg cost &divide;
        pass-rate is the headline efficiency metric.</p></div>"""
        variability = _render_variability(data.get("variability", []))
        body = f"""
        <p class="lede">Three deterministic computer-use flows across different application types, each
        monitored for cost, accuracy, and token usage. One row per run; the numbers accumulate over history.</p>
        {totals_html}
        <h2>Per-flow accuracy</h2>
        <div class="cards">{cards}</div>
        <h2>By app type (cross-type comparison)</h2>
        <table><thead><tr><th>app type</th><th>runs</th><th>err</th><th>pass%</th><th>$/run</th>
        <th>$/success</th><th>tokens</th><th>steps</th><th>time/run</th></tr></thead>
        <tbody>{_html_rows(data["by_app_type"])}</tbody></table>
        <h2>By flow</h2>
        <table><thead><tr><th>flow</th><th>runs</th><th>err</th><th>pass%</th><th>$/run</th>
        <th>$/success</th><th>tokens</th><th>steps</th><th>time/run</th></tr></thead>
        <tbody>{_html_rows(data["by_flow"])}</tbody></table>
        {variability}
        {architecture}
        {cost_usage}
        <h2>Recent runs</h2>
        <table><thead><tr><th>flow</th><th>app type</th><th>status</th><th>cost</th>
        <th>tokens</th><th>steps</th><th>latency</th><th>when</th></tr></thead>
        <tbody>{recent}</tbody></table>
        <footer>Generated by <code>report.py</code> from <code>runs.db</code> &middot;
        <a href="index.html">Home</a> &middot; <a href="brief.html">Agent</a> &middot;
        <a href="flows.html">Framework</a> &middot; architecture &amp; design notes:
        <code>docs/ARCHITECTURE.md</code>, <code>docs/PLAN.md</code></footer>
        """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>COA flow metrics</title><style>{_CSS}</style></head>
<body>
<nav class="sitenav"><span class="brand">coa-test</span><span class="sep">·</span><a href="index.html">Home</a><a href="brief.html">Agent</a><a href="flows.html">Framework</a><span class="cur">Results</span></nav>
<h1>Computer-use agent — flow metrics</h1>
<p class="sub">cost · accuracy · token usage, accumulated per run. Generated {html.escape(gen)}</p>
{body}
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Report COA metrics from runs.db.")
    ap.add_argument("--db", default=str(metrics_db.DEFAULT_DB))
    ap.add_argument("--html-out", default=DEFAULT_HTML_OUT)
    ap.add_argument("--no-html", action="store_true", help="print the table only")
    args = ap.parse_args()

    conn = metrics_db.connect(args.db)
    data = gather(conn, generated=datetime.now().isoformat(timespec="seconds"))
    conn.close()

    print(render_cli(data))
    if not args.no_html:
        from pathlib import Path
        out = Path(args.html_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(data))
        print(f"\nHTML report: {out}")


if __name__ == "__main__":
    sys.exit(main())
