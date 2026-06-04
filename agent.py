"""Computer-use agent: orchestrates the vision loop, native tools, and HITL gates.

Usage:
    python -u agent.py -y "install the gc12.dmg from my Desktop"
    python -u agent.py --diagnose
    python -u agent.py --review-plan -y "install the gc12.dmg from my Desktop"
"""
import argparse, os, sys, time
from datetime import datetime
from pathlib import Path

import anthropic, pyautogui
from dotenv import load_dotenv

import diagnostics
import native_actions
import review
from context_window import compact_messages, mark_rolling_cache
from recorder import Recorder
from screen import image_block, shoot, text_block, tool_result, TARGET_W, TARGET_H

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

MODEL = "claude-sonnet-4-6"
TOOL_TYPE = "computer_20251124"
BETA_HEADER = "computer-use-2025-11-24"
MAX_STEPS = 80
DEFAULT_IMAGE_HISTORY = 4

SYSTEM_PROMPT = """You are a QA automation agent operating a macOS desktop via the computer tool.

Rules:
- Take ONE action at a time. After each action you receive a fresh screenshot.
- Briefly state what you observe and what you'll do next before each action.
- If a password / Touch ID / system security prompt appears, STOP. Do NOT type any password.
  Report exactly what you see and end your message with 'BLOCKED'.
- If a step doesn't work after 2 retries, try a different approach
  (keyboard shortcut, menu bar, or a macos tool action). Do not rely on Spotlight/cmd+space.
- Prefer normal computer vision/click actions for observing and navigating.
- Use the macos tool for reliable host primitives: opening a known path, hiding VSCode/Electron,
  installing an .app from a mounted .dmg when Finder drag-and-drop fails, ejecting a volume, or
  verifying that an app exists in /Applications.
- A human reviewer may reject a tool call. If a tool_result says REJECTED or BLOCKED, do not
  retry the same call; propose a safer alternative or end the run.
- When the task is fully verified complete, end your final message with 'TASK COMPLETE'.
"""

# System prompt as a cacheable block. The tools + system prefix is identical on
# every turn, so one ephemeral breakpoint here lets the API serve it from cache
# after the first request instead of re-billing it each step.
SYSTEM_BLOCKS = [{
    "type": "text",
    "text": SYSTEM_PROMPT,
    "cache_control": {"type": "ephemeral"},
}]

PLAN_SYSTEM_PROMPT = """You are planning a macOS computer-use task for a human reviewer.

You will be shown a screenshot of the user's current desktop and a task. Produce a SHORT
numbered plan (4-8 steps). For each step, include:
  - what you'll observe or click
  - which tool you intend to use (computer.* or macos.*)
  - the risk level (observe / low / medium / high)

End with one line:
  Expected high-risk actions: <comma-separated list, or 'none'>

Do not take any actions. Output only the plan."""

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.15

client = anthropic.Anthropic()


def execute_computer(action: str, **kw):
    """Run one computer-use action via pyautogui."""
    try:
        coord = kw.get("coordinate")
        if   action == "screenshot": pass
        elif action == "mouse_move": pyautogui.moveTo(*coord, duration=0.3)
        elif action == "left_click": pyautogui.click(*coord) if coord else pyautogui.click()
        elif action == "right_click": pyautogui.rightClick(*coord) if coord else pyautogui.rightClick()
        elif action == "double_click": pyautogui.doubleClick(*coord) if coord else pyautogui.doubleClick()
        elif action == "triple_click": pyautogui.tripleClick(*coord) if coord else pyautogui.tripleClick()
        elif action == "middle_click": pyautogui.middleClick(*coord) if coord else pyautogui.middleClick()
        elif action == "left_click_drag":
            start = kw.get("start_coordinate")
            if start:
                pyautogui.moveTo(*start, duration=0.15)
            pyautogui.mouseDown(button="left")
            time.sleep(0.2)
            pyautogui.moveTo(*coord, duration=0.9)
            time.sleep(0.2)
            pyautogui.mouseUp(button="left")
        elif action == "left_mouse_down":
            if coord:
                pyautogui.moveTo(*coord, duration=0.15)
            pyautogui.mouseDown(button="left")
        elif action == "left_mouse_up":
            if coord:
                pyautogui.moveTo(*coord, duration=0.15)
            pyautogui.mouseUp(button="left")
        elif action == "type": pyautogui.write(kw["text"], interval=0.03)
        elif action == "key":
            keys = kw["text"].lower().replace("super", "command").split("+")
            pyautogui.hotkey(*keys) if len(keys) > 1 else pyautogui.press(keys[0])
        elif action == "hold_key":
            keys = kw["text"].lower().split("+"); dur = kw.get("duration", 1.0)
            for k in keys: pyautogui.keyDown(k)
            time.sleep(dur)
            for k in reversed(keys): pyautogui.keyUp(k)
        elif action == "scroll":
            if coord: pyautogui.moveTo(*coord)
            sign = {"up": 1, "down": -1, "left": 0, "right": 0}[kw.get("scroll_direction", "down")]
            pyautogui.scroll(sign * kw.get("scroll_amount", 3) * 100)
        elif action == "wait": time.sleep(kw.get("duration", 1.0))
        elif action == "cursor_position":
            p = pyautogui.position()
            return None, f"Cursor at ({p.x}, {p.y})", False
        else:
            return None, f"Unsupported action: {action}", True

        time.sleep(0.6)
        img, b64 = shoot()
        return img, [image_block(b64)], False
    except Exception as e:
        return None, f"Action failed: {e!r}", True


def execute_macos(action: str, **kw):
    try:
        if action == "hide_vscode":
            is_error, content = False, native_actions.hide_vscode()[1]
        elif action == "activate_app":
            rc, content = native_actions.focus_app(kw["app_name"])
            is_error = rc != 0
        elif action == "open_path":
            rc, content = native_actions.open_path(kw["path"])
            is_error = rc != 0
        elif action == "run_applescript":
            rc, content = native_actions.run_osascript(kw["script"])
            is_error = rc != 0
        elif action == "install_app_from_dmg":
            is_error, content = native_actions.install_app_from_dmg(
                dmg_path=kw.get("dmg_path", "~/Desktop/gc12.dmg"),
                app_name=kw.get("app_name", ""),
                volume_name=kw.get("volume_name", ""),
            )
        elif action == "eject_volume":
            is_error, content = native_actions.eject_volume(
                volume_name=kw.get("volume_name", ""),
                dmg_path=kw.get("dmg_path", ""),
            )
        elif action == "verify_app_installed":
            is_error, content = native_actions.verify_app_installed(kw["app_name"])
        elif action == "safe_shell":
            is_error, content = native_actions.safe_shell(kw["command"])
        else:
            return None, f"Unsupported macos action: {action}", True

        time.sleep(0.8)
        img, b64 = shoot()
        if is_error:
            # API requires text-only content when is_error. Keep the screenshot
            # on disk (caller saves `img`) but don't send the image to the model.
            return img, [text_block(content or "Action failed.")], is_error
        suffix = f"{content}\n\nScreenshot after action:" if content else "Screenshot after action:"
        return img, [text_block(suffix), image_block(b64)], is_error
    except Exception as e:
        return None, f"macos action failed: {e!r}", True


def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "computer":
        return execute_computer(**tool_input)
    if tool_name == "macos":
        return execute_macos(**tool_input)
    return None, f"Unsupported tool: {tool_name}", True


TOOL = {
    "type": TOOL_TYPE,
    "name": "computer",
    "display_width_px": TARGET_W,
    "display_height_px": TARGET_H,
    "display_number": 1,
}

MACOS_TOOL = {
    "type": "custom",
    "name": "macos",
    "description": (
        "Reliable macOS host primitives to complement visual computer control. "
        "Use this tool when a known path/app/volume operation is safer than raw clicks, "
        "or when Finder drag-and-drop fails. The install_app_from_dmg action mounts a DMG "
        "if needed, finds an .app bundle at the mounted volume root, and copies it to /Applications."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "hide_vscode",
                    "activate_app",
                    "open_path",
                    "run_applescript",
                    "install_app_from_dmg",
                    "eject_volume",
                    "verify_app_installed",
                    "safe_shell",
                ],
            },
            "app_name": {"type": "string"},
            "path": {"type": "string"},
            "script": {"type": "string"},
            "dmg_path": {"type": "string"},
            "volume_name": {"type": "string"},
            "command": {"type": "string"},
        },
        "required": ["action"],
    },
}


def request_plan(task: str, screenshot_b64: str) -> str:
    """Single planning call — no tools enabled, just narrative."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=PLAN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"Task: {task}\n\nProduce the plan."},
            image_block(screenshot_b64),
        ]}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts).strip() or "(planner returned no text)"


def _last_screenshot_path(rec: Recorder):
    pngs = sorted(rec.dir.glob("*.png"))
    return pngs[-1] if pngs else None


_USAGE_FIELDS = (
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
)


def _add_usage(totals: dict, usage) -> None:
    """Fold one API response's `usage` object into the running per-run totals."""
    if usage is None:
        return
    for field in _USAGE_FIELDS:
        value = getattr(usage, field, None)
        if value:
            totals[field] = totals.get(field, 0) + value


def run_task(
    task: str,
    *,
    max_steps=MAX_STEPS,
    image_history=DEFAULT_IMAGE_HISTORY,
    hide_vscode_on_start=True,
    policy: review.ReviewPolicy,
    do_plan_approval: bool,
):
    rec = Recorder()
    policy.attach(rec.dir)
    print(f"\nRecording to: {rec.dir}\n")
    rec.write(f"**Task:** {task}")
    rec.write(f"**Review mode:** `{policy.mode}` · final-review: {policy.require_final_review}")

    agent_claim = "(no final text)"
    rejected_actions = []
    errored_actions = []
    end_reason = "unknown"
    machine_verification = {}
    token_usage = {field: 0 for field in _USAGE_FIELDS}

    try:
        img, b64 = shoot()
        rec.save_screenshot(img, "initial")
        rec.heading("initial screenshot")

        if do_plan_approval:
            print("\nGenerating plan…")
            plan_text = request_plan(task, b64)
            print("\n[Plan]\n" + plan_text)
            rec.write("**Proposed plan:**\n\n" + plan_text)
            approved, reason = review.request_plan_approval(policy, plan_text, task)
            if not approved:
                print(f"\nPlan rejected ({reason}). Aborting before any actions.")
                rec.write(f"**Plan rejected** ({reason})")
                end_reason = "plan_rejected"
                machine_verification = {"ok": False, "blocked": True, "reason": "plan_rejected"}
                return

        if hide_vscode_on_start:
            rec.write("**Prep:** hiding VSCode/Electron and activating Finder")
            _, body = native_actions.hide_vscode()
            rec.write(f"prep result: `{body}`")
            policy.vscode_hidden = True
            img, b64 = shoot()
            rec.save_screenshot(img, "after_hide_vscode")

        messages = [{"role": "user", "content": [
            {"type": "text", "text": task},
            image_block(b64),
        ]}]

        for _ in range(max_steps):
            compact_messages(messages, keep_last_images=image_history)
            mark_rolling_cache(messages)
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_BLOCKS,
                tools=[TOOL, MACOS_TOOL],
                messages=messages,
                betas=[BETA_HEADER],
            )
            _add_usage(token_usage, getattr(resp, "usage", None))

            last_text_this_turn = ""
            for block in resp.content:
                if block.type == "text":
                    last_text_this_turn = block.text
                    print(f"\n[Claude] {block.text}")
                    rec.write(f"**Claude:** {block.text}")
                elif block.type == "tool_use":
                    name = getattr(block, "name", "computer")
                    a = block.input.get("action", "?")
                    print(f"[Action] {name}.{a} {block.input}")

            if last_text_this_turn:
                agent_claim = last_text_this_turn

            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                print("\n[Done] end_turn")
                rec.write("**Stop:** end_turn")
                end_reason = "end_turn"
                break

            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                name = getattr(block, "name", "computer")
                a = block.input.get("action", "?")
                tool_input = dict(block.input)

                last_png = _last_screenshot_path(rec)
                approved, risk, reason = review.require_approval(
                    policy,
                    step=rec.step + 1,
                    tool_name=name,
                    tool_input=tool_input,
                    screenshot_path=last_png.name if last_png else None,
                    last_screenshot_png=last_png,
                )
                rec.heading(f"action: `{name}.{a}` ({risk})")
                rec.write(f"input: `{tool_input}`")
                rec.write(f"gate: {'approved' if approved else 'rejected'} — {reason}")

                if not approved:
                    rejected_actions.append({
                        "step": rec.step, "tool": name, "action": a,
                        "risk": risk, "reason": reason,
                    })
                    results.append(tool_result(block.id, reason, is_error=True))
                    continue

                img, content, is_error = execute_tool(name, tool_input)
                if img is not None:
                    rec.save_screenshot(img, a)
                else:
                    rec.step += 1
                if is_error:
                    rec.write(f"error: {content}")
                    errored_actions.append({
                        "step": rec.step, "tool": name, "action": a,
                        "risk": risk,
                    })
                results.append(tool_result(block.id, content, is_error=is_error))

            if not results:
                print("\n[Bail] no tool calls and not end_turn")
                rec.write("**Stop:** no tool calls returned and stop_reason was not end_turn")
                end_reason = "no_tool_calls"
                break

            messages.append({"role": "user", "content": results})
        else:
            print(f"\n[Bail] hit MAX_STEPS={max_steps}")
            rec.write(f"**Stop:** MAX_STEPS={max_steps}")
            end_reason = "max_steps"
    finally:
        machine_verification = {
            "end_reason": end_reason,
            "ok": end_reason == "end_turn"
                  and "TASK COMPLETE" in agent_claim
                  and not rejected_actions
                  and not errored_actions,
            "blocked": bool(rejected_actions) or end_reason == "plan_rejected",
            "agent_ended_with_task_complete": "TASK COMPLETE" in agent_claim,
            "rejected_actions": rejected_actions,
            "errored_actions": errored_actions,
            "step_count": rec.step,
            "token_usage": token_usage,
        }

        screenshots = sorted(p.name for p in rec.dir.glob("*.png"))
        approvals_rel = None
        if policy.approvals_log_jsonl is not None:
            approvals_rel = str(policy.approvals_log_jsonl.relative_to(rec.dir))

        summary = (
            f"Task: {task}\n"
            f"Agent claim: {agent_claim[:200]}\n"
            f"End reason: {end_reason}\n"
            f"Rejected: {len(rejected_actions)} · Errors: {len(errored_actions)}\n"
            f"Screenshots: {len(screenshots)}"
        )
        human_decision = review.request_final_review(policy, summary)

        md, js = review.write_final_report(
            rec.dir,
            task=task,
            agent_claim=agent_claim,
            machine_verification=machine_verification,
            transcript_name="transcript.md",
            screenshots=screenshots,
            approvals_path=approvals_rel,
            human_decision=human_decision,
        )

        rec.write(f"**Final report:** `{md.name}` / `{js.name}`")
        rec.close()
        print(f"\nTranscript: {rec.dir / 'transcript.md'}")
        print(f"Screenshots: {rec.dir}")
        print(f"Final report: {md}")
        print(f"Final report (json): {js}")
        print(
            "Tokens: "
            f"in={token_usage['input_tokens']} "
            f"out={token_usage['output_tokens']} "
            f"cache_read={token_usage['cache_read_input_tokens']} "
            f"cache_write={token_usage['cache_creation_input_tokens']}"
        )


def _run_diagnose(dmg: str = "", app: str = "") -> None:
    """Shared diagnose entry point so the REPL `/diagnose` command stays in sync with `--diagnose`."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = Path("runs") / ts
    data = diagnostics.collect(target_dmg=dmg, target_app=app)
    md, js = diagnostics.write(out_dir, data)
    print(f"Diagnostics written:\n  {md}\n  {js}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", nargs="?", help="Natural-language task for the agent")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip pre-run confirms and assume-yes for low/medium gates")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--image-history", type=int, default=DEFAULT_IMAGE_HISTORY)
    parser.add_argument("--no-hide-vscode", action="store_true", help="Do not hide VSCode/Electron before the first screenshot")
    parser.add_argument("--diagnose", action="store_true", help="Run preflight diagnostics and exit (no Anthropic calls)")
    parser.add_argument("--approve-pending", metavar="RUN_DIR", default=None,
                        help="Out-of-band escape hatch for the file-fallback: walk pending-*.json in a previous run dir, prompt for each, log to that run's approvals.jsonl, and exit.")
    parser.add_argument("--diagnose-dmg", default="", help="Optional DMG path to verify during --diagnose")
    parser.add_argument("--diagnose-app", default="", help="Optional app name to look up during --diagnose")
    parser.add_argument("--review-mode", choices=review.REVIEW_MODES, default="high_risk",
                        help="off | plan | high_risk (default) | every_action")
    parser.add_argument("--review-plan", action="store_true", help="Generate a plan and require approval before any actions")
    parser.add_argument("--no-final-review", action="store_true", help="Skip the post-run human pass/fail prompt (the only way to skip Gate C)")
    parser.add_argument("--allow-typing-anything", action="store_true",
                        help="Skip the password/sudo-in-typed-text heuristic — needed when typing license keys, API tokens, etc.")
    args = parser.parse_args()

    if args.diagnose:
        _run_diagnose(args.diagnose_dmg, args.diagnose_app)
        return

    if args.approve_pending:
        run_dir = Path(args.approve_pending)
        if not run_dir.is_dir():
            sys.exit(f"--approve-pending: not a directory: {run_dir}")
        counts = review.approve_pending(run_dir)
        print(
            f"Reviewed {counts['reviewed']} pending action(s): "
            f"{counts['approved']} approved, {counts['rejected']} rejected, "
            f"{counts['skipped']} skipped."
        )
        return

    def _make_policy() -> review.ReviewPolicy:
        return review.ReviewPolicy(
            mode=args.review_mode,
            require_final_review=not args.no_final_review,
            assume_yes=args.yes,
            allow_typing_anything=args.allow_typing_anything,
        )

    if not args.task:
        # Interactive REPL — opt-in by omitting the positional task arg.
        import interactive
        _make_policy().preflight()  # once, before any potential VSCode hide
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("(warning) ANTHROPIC_API_KEY not set — /diagnose and /approve-pending still work; tasks will fail at API call time.")

        def _repl_run_task(task: str, *, policy: review.ReviewPolicy) -> None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("ANTHROPIC_API_KEY not set; cannot run task. Add it to .env and restart.")
                return
            run_task(
                task,
                max_steps=args.max_steps,
                image_history=args.image_history,
                hide_vscode_on_start=not args.no_hide_vscode,
                policy=policy,
                do_plan_approval=args.review_plan or args.review_mode == "plan",
            )

        interactive.run_repl(
            runs_dir=Path("runs"),
            policy_factory=_make_policy,
            run_task_callable=_repl_run_task,
            run_diagnose_callable=lambda: _run_diagnose(args.diagnose_dmg, args.diagnose_app),
        )
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. Add it to .env (ANTHROPIC_API_KEY=sk-ant-...) or export it.")

    policy = _make_policy()
    policy.preflight()

    print("Claude will take over your mouse and keyboard.")
    print("    Don't touch the machine while it runs.")
    print("    Abort by slamming the cursor into any screen corner.\n")
    print(f"Review mode: {policy.mode} · final-review: {policy.require_final_review} · plan-approval: {args.review_plan}\n")
    if not args.yes:
        input("Press Enter to start, Ctrl+C to abort... ")

    run_task(
        args.task,
        max_steps=args.max_steps,
        image_history=args.image_history,
        hide_vscode_on_start=not args.no_hide_vscode,
        policy=policy,
        do_plan_approval=args.review_plan or args.review_mode == "plan",
    )


if __name__ == "__main__":
    main()
