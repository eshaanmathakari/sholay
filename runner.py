"""Deterministic flow runner — drives a YAML playbook step-by-step and measures it.

For each step in the spec, the runner hands the agent ONE sub-goal, lets it work via
the existing computer/macos tools until it ends its turn (or says STEP DONE), and
records that step's tokens / time / actions / retries. Conversation history (and the
rolling prompt cache) carries across steps. The final step emits a JSON object that
the flow's oracle scores. One run → one row in runs.db + a final_report.json on disk.

Runs are autonomous (no human gates) — these are reviewed demo/showcase flows
(see docs/PLAN.md D6). The gated, human-in-the-loop experience still lives in
agent.py; wiring demo-mode gates into the runner is a later step.

Usage:
    python runner.py flows/tradingview.yaml
    python runner.py flows/tradingview.yaml --max-step-actions 25
"""
import argparse
import importlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import native_actions
import pricing
import metrics_db
from flows import loader
from context_window import compact_messages, mark_rolling_cache
from recorder import Recorder
from screen import image_block, shoot, text_block, tool_result

# Reuse the agent's primitives so action semantics are identical to the live agent.
from agent import (
    client, MODEL, BETA_HEADER, TOOL, MACOS_TOOL,
    execute_tool, _add_usage, _USAGE_FIELDS,
)

RUNNER_SYSTEM = """You are a deterministic QA automation agent operating a macOS desktop via the computer tool.

You are given ONE step at a time from a fixed playbook. Work ONLY on the current step.

Rules:
- Take ONE action at a time. After each action you receive a fresh screenshot.
- Briefly say what you observe and what you'll do next before each action.
- Use the `macos` tool for reliable host primitives (activate an app, open a known path)
  when that is more reliable than clicking. Do not rely on Spotlight / cmd+space.
- Dismiss cookie/login/popup overlays that block the task.
- Typing: enter whole strings with ONE `type` action. Do NOT press individual letter keys.
  To fix/replace a field's text, click the field once, press key `cmd+a` to select all, then
  `type` the correct value in one go — never delete character-by-character.
- Recovery: if an unexpected panel or overlay appears (Control Center, emoji picker, Spotlight,
  a right-click menu, a PDF/attachment preview), press key `escape` ONCE (or click an empty
  area of the target app), then continue. Do NOT keep typing into it.
- Focus: before typing into a field or cell, click directly on it first so keystrokes can't
  leak to another app as system shortcuts.
- Only fill existing fields and click existing controls. NEVER create, rename, or delete a file,
  a database column/property, or an app setting — if something you expect is missing, do not
  build it; note it and do the best you can with what exists.
- Do not open email attachments or click links.
- If a password / Touch ID / system security prompt appears, STOP. Do not type any password.
- When the CURRENT STEP's goal is achieved, STOP taking actions and end your message with the
  exact token on its own line: STEP DONE
- Do not work ahead to later steps.
"""

RUNNER_SYSTEM_BLOCKS = [{
    "type": "text", "text": RUNNER_SYSTEM, "cache_control": {"type": "ephemeral"},
}]

STEP_DONE = "STEP DONE"


def _run_step(messages, goal, *, rec, token_usage, image_history, max_step_actions, extra_text=None):
    """Drive one sub-goal to completion; return its per-step metrics + last text."""
    content = []
    if extra_text:
        content.append(text_block(extra_text))
    content.append(text_block(f"CURRENT STEP: {goal}"))
    img, b64 = shoot()
    content.append(image_block(b64))
    messages.append({"role": "user", "content": content})

    actions = retries = 0
    step_usage = {f: 0 for f in _USAGE_FIELDS}
    last_text = ""
    completed = False
    start = time.time()

    for _ in range(max_step_actions):
        compact_messages(messages, keep_last_images=image_history)
        mark_rolling_cache(messages)
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=4096, system=RUNNER_SYSTEM_BLOCKS,
            tools=[TOOL, MACOS_TOOL], messages=messages, betas=[BETA_HEADER],
        )
        _add_usage(token_usage, getattr(resp, "usage", None))
        _add_usage(step_usage, getattr(resp, "usage", None))

        made_tool_call = False
        last_text_this = ""
        for block in resp.content:
            if block.type == "text":
                last_text_this = block.text
                print(f"\n[Claude] {block.text}")
                rec.write(f"**Claude:** {block.text}")
            elif block.type == "tool_use":
                made_tool_call = True
        if last_text_this:
            last_text = last_text_this
        messages.append({"role": "assistant", "content": resp.content})

        done = (resp.stop_reason == "end_turn") or (STEP_DONE in last_text_this and not made_tool_call)

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            name = getattr(block, "name", "computer")
            a = block.input.get("action", "?")
            tool_input = dict(block.input)
            rec.heading(f"action: `{name}.{a}`")
            rec.write(f"input: `{tool_input}`")
            img, tcontent, is_error = execute_tool(name, tool_input)
            actions += 1
            if img is not None:
                rec.save_screenshot(img, a)
            else:
                rec.step += 1
            if is_error:
                retries += 1
                rec.write(f"error: {tcontent}")
            results.append(tool_result(block.id, tcontent, is_error=is_error))

        if results:
            messages.append({"role": "user", "content": results})
        if done and not results:
            completed = True
            break

    return {
        "actions": actions, "retries": retries, "usage": step_usage,
        "latency": time.time() - start, "last_text": last_text, "completed": completed,
    }


def _extract_emit(text: str):
    """Pull the emitted JSON object out of the final step's text. Returns dict or None."""
    if not text:
        return None
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])
    for blob in candidates:
        try:
            return json.loads(blob)
        except Exception:
            continue
    return None


def _emit_instruction(spec) -> str:
    keys = ", ".join(spec.emit_schema) if spec.emit_schema else "the required fields"
    text = (
        "This is the final step. Produce the required result, then end your message with a "
        f"single fenced ```json code block containing an object with keys: {keys}. "
        f"After the code block, write {STEP_DONE} on its own line."
    )
    if spec.pattern_reference:
        catalog = Path(spec.pattern_reference).read_text()
        text += "\n\nTECHNICAL-ANALYSIS PATTERN CATALOG — use these exact names when flagging patterns:\n" + catalog
    return text


def run_flow(spec_path, *, image_history=4, max_step_actions=20, hide_vscode=True) -> dict:
    spec = loader.load(spec_path)
    rec = Recorder()
    print(f"\nFlow: {spec.name}  ({spec.app_type})  →  recording to {rec.dir}\n")
    rec.write(f"**Flow:** {spec.name} · app_type: {spec.app_type} · mode: {spec.mode}")

    if hide_vscode:
        native_actions.hide_vscode()

    token_usage = {f: 0 for f in _USAGE_FIELDS}
    messages: list = []
    step_results = []
    overall_start = time.time()

    intro = (
        f"FLOW: {spec.name}. You will be guided one step at a time. "
        "Complete each step, then end with STEP DONE."
    )
    last_idx = len(spec.steps) - 1
    for i, step in enumerate(spec.steps):
        rec.write(f"### Step {i + 1}/{len(spec.steps)}: {step.id}")
        extra = intro if i == 0 else None
        if i == last_idx and spec.emit_schema:
            instr = _emit_instruction(spec)
            extra = f"{extra}\n\n{instr}" if extra else instr
        sr = _run_step(
            messages, step.goal, rec=rec, token_usage=token_usage,
            image_history=image_history, max_step_actions=max_step_actions, extra_text=extra,
        )
        step_results.append((step, sr))

    emit = _extract_emit(step_results[-1][1]["last_text"]) if step_results else None

    if emit is None:
        status, fact_match, reasons = "fail", None, ["no parseable JSON emit from the final step"]
    else:
        oracle = importlib.import_module(f"oracles.{spec.oracle}")
        cfg = {**spec.oracle_config, "pattern_reference": spec.pattern_reference}
        res = oracle.run_oracle(emit, cfg)
        status, fact_match, reasons = res.status, res.fact_match, res.reasons

    total_actions = sum(sr["actions"] for _, sr in step_results)
    total_retries = sum(sr["retries"] for _, sr in step_results)
    latency = time.time() - overall_start
    ts = datetime.now().isoformat(timespec="seconds")

    row = {
        "run_id": rec.dir.name, "ts": ts, "flow": spec.name, "app_type": spec.app_type,
        "model": spec.model, "mode": spec.mode, "status": status,
        "steps": total_actions, "steps_expected": spec.steps_expected,
        "retries": total_retries, "misclicks": total_retries,
        "in_tok": token_usage["input_tokens"], "out_tok": token_usage["output_tokens"],
        "cache_read": token_usage["cache_read_input_tokens"],
        "cache_write": token_usage["cache_creation_input_tokens"],
        "cost_usd": pricing.cost_usd(token_usage, spec.model),
        "latency_s": round(latency, 1),
        "fact_match": None if fact_match is None else int(bool(fact_match)),
        "run_dir": str(rec.dir),
    }

    conn = metrics_db.connect()
    metrics_db.insert_run(conn, row)
    for i, (step, sr) in enumerate(step_results):
        u = sr["usage"]
        metrics_db.insert_step(conn, {
            "run_id": row["run_id"], "step_idx": i, "goal": step.goal,
            "steps": sr["actions"], "retries": sr["retries"],
            "in_tok": u["input_tokens"], "out_tok": u["output_tokens"],
            "cache_read": u["cache_read_input_tokens"], "cache_write": u["cache_creation_input_tokens"],
            "latency_s": round(sr["latency"], 1), "ok": int(sr["completed"]),
        })
    conn.close()

    report = {
        "run_id": row["run_id"], "flow": spec.name, "app_type": spec.app_type,
        "status": status, "fact_match": fact_match, "oracle_reasons": reasons,
        "emit": emit, "token_usage": token_usage, "cost_usd": row["cost_usd"],
        "latency_s": row["latency_s"], "steps": total_actions, "retries": total_retries,
        "step_metrics": [
            {"step_idx": i, "id": step.id, "actions": sr["actions"], "retries": sr["retries"],
             "latency_s": round(sr["latency"], 1), "completed": sr["completed"]}
            for i, (step, sr) in enumerate(step_results)
        ],
    }
    (rec.dir / "final_report.json").write_text(json.dumps(report, indent=2))
    rec.write(f"**Status:** {status} · cost ${row['cost_usd']:.4f} · "
              f"{total_actions} actions · {row['latency_s']}s")
    for r in reasons:
        rec.write(f"- {r}")
    rec.close()

    print("\n" + "=" * 60)
    print(f"STATUS: {status}   fact_match={fact_match}   cost=${row['cost_usd']:.4f}")
    print(f"tokens in={token_usage['input_tokens']} out={token_usage['output_tokens']} "
          f"cache_read={token_usage['cache_read_input_tokens']} "
          f"cache_write={token_usage['cache_creation_input_tokens']}")
    print(f"actions={total_actions} retries={total_retries} latency={row['latency_s']}s")
    for r in reasons:
        print(f"  - {r}")
    print(f"Artifacts: {rec.dir}")
    print("=" * 60)
    return report


def main():
    ap = argparse.ArgumentParser(description="Run a deterministic COA flow and record its metrics.")
    ap.add_argument("spec", help="path to a flow spec, e.g. flows/tradingview.yaml")
    ap.add_argument("--max-step-actions", type=int, default=20,
                    help="max tool actions per step before giving up on it")
    ap.add_argument("--image-history", type=int, default=4,
                    help="how many recent screenshots to keep in the API history")
    ap.add_argument("--no-hide-vscode", action="store_true", help="don't hide VSCode at start")
    args = ap.parse_args()
    run_flow(
        args.spec,
        image_history=args.image_history,
        max_step_actions=args.max_step_actions,
        hide_vscode=not args.no_hide_vscode,
    )


if __name__ == "__main__":
    sys.exit(main())
