"""Preflight diagnostics — capture host state without calling Anthropic.

Most of the bugs in early runs (coordinate mismatch, focus drift, 413 history
overflow, missing TCC permissions) are cheap to detect before paying for an
agent run. Running these checks up front means failures point at the host, not
the model.
"""
import json, os, plistlib, subprocess
from datetime import datetime
from pathlib import Path

import pyautogui

import native_actions
from screen import shoot, TARGET_W, TARGET_H


def _frontmost_process() -> dict:
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return {"ok": proc.returncode == 0, "name": proc.stdout.strip(), "stderr": proc.stderr.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"ok": False, "error": repr(e)}


def _screencapture_probe() -> dict:
    try:
        img, b64 = shoot()
        return {
            "ok": True,
            "size_after_resize": list(img.size),
            "base64_bytes": len(b64),
        }
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _raw_screencapture_size() -> dict:
    import tempfile
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        proc = subprocess.run(["screencapture", "-x", tmp], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0 or os.path.getsize(tmp) == 0:
            return {"ok": False, "rc": proc.returncode, "stderr": proc.stderr.strip()}
        with Image.open(tmp) as img:
            raw_w, raw_h = img.size
        return {"ok": True, "raw_size": [raw_w, raw_h]}
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def _vscode_processes() -> dict:
    found = []
    for name in ("Electron", "Code", "Visual Studio Code"):
        proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            found.append(name)
    return {"running": found}


def _target_dmg(path: str) -> dict:
    if not path:
        return {"checked": False}
    expanded = os.path.expanduser(path)
    return {"checked": True, "path": expanded, "exists": os.path.exists(expanded)}


def _target_app(name: str) -> dict:
    if not name:
        return {"checked": False}
    is_error, body = native_actions.verify_app_installed(name)
    return {"checked": True, "app_name": name, "installed": not is_error, "detail": body}


def collect(*, target_dmg: str = "", target_app: str = "") -> dict:
    raw = _raw_screencapture_size()
    scale = None
    if raw.get("ok"):
        rw, rh = raw["raw_size"]
        if TARGET_W > 0 and TARGET_H > 0:
            scale = round(rw / TARGET_W, 3)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pyautogui_size": [TARGET_W, TARGET_H],
        "raw_screencapture": raw,
        "retina_scale": scale,
        "screencapture_resize_probe": _screencapture_probe(),
        "cursor_position": list(pyautogui.position()),
        "frontmost_process": _frontmost_process(),
        "mounted_volumes": native_actions.attached_volumes(),
        "vscode_processes": _vscode_processes(),
        "target_dmg": _target_dmg(target_dmg),
        "target_app": _target_app(target_app),
    }


def render_md(d: dict) -> str:
    lines = [
        f"# Diagnostics — {d['generated_at']}",
        "",
        f"- pyautogui size: `{d['pyautogui_size']}`",
        f"- raw screencapture: `{d['raw_screencapture']}`",
        f"- retina scale: `{d['retina_scale']}`",
        f"- screencapture probe: `{d['screencapture_resize_probe']}`",
        f"- cursor position: `{d['cursor_position']}`",
        f"- frontmost process: `{d['frontmost_process']}`",
        f"- VSCode/Electron processes: `{d['vscode_processes']}`",
        f"- mounted volumes: `{d['mounted_volumes']}`",
        f"- target dmg: `{d['target_dmg']}`",
        f"- target app: `{d['target_app']}`",
    ]
    return "\n".join(lines) + "\n"


def write(out_dir: Path, data: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "diagnostics.json"
    md_path = out_dir / "diagnostics.md"
    json_path.write_text(json.dumps(data, indent=2, default=str))
    md_path.write_text(render_md(data))
    return md_path, json_path
