"""Reliable macOS host primitives that the agent can call as a `macos` tool.

These exist because raw click sequences against Finder are fragile: focus drifts,
drag dispatch is unreliable, and DMG mount points race the UI. Each helper here
returns `(rc_or_error_flag, body_text)` so the orchestrator can surface a clean
tool_result to the model without leaking subprocess details into the prompt.
"""
import json, os, plistlib, subprocess
from pathlib import Path

SHELL_TIMEOUT = 90


def run_cmd(cmd, timeout=SHELL_TIMEOUT):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    body = f"rc={proc.returncode}"
    if out:
        body += f"\nstdout:\n{out}"
    if err:
        body += f"\nstderr:\n{err}"
    return proc.returncode, body


def run_osascript(script: str, timeout=30):
    return run_cmd(["osascript", "-e", script], timeout=timeout)


def focus_app(app_name: str):
    script = f'tell application {json.dumps(app_name)} to activate'
    return run_osascript(script)


def hide_process(process_name: str):
    script = f'''
tell application "System Events"
    set visible of every process whose name is {json.dumps(process_name)} to false
end tell
'''
    return run_osascript(script)


def hide_vscode():
    results = []
    for name in ("Electron", "Code", "Visual Studio Code"):
        _, body = hide_process(name)
        results.append(f"{name}: {body}")
    focus_app("Finder")
    return 0, "\n\n".join(results)


def open_path(path: str):
    return run_cmd(["open", os.path.expanduser(path)], timeout=30)


def attached_volumes():
    proc = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, timeout=15)
    if proc.returncode != 0:
        return []
    data = plistlib.loads(proc.stdout)
    volumes = []
    for image in data.get("images", []):
        image_path = image.get("image-path")
        for entity in image.get("system-entities", []):
            mount_point = entity.get("mount-point")
            if mount_point:
                volumes.append({"image_path": image_path, "mount_point": mount_point})
    return volumes


def attach_dmg(dmg_path: str):
    dmg_path = os.path.expanduser(dmg_path)
    existing = [v for v in attached_volumes() if v.get("image_path") == dmg_path]
    if existing:
        return 0, "Already mounted:\n" + "\n".join(v["mount_point"] for v in existing)
    return run_cmd(["hdiutil", "attach", dmg_path], timeout=120)


def find_volume_for_dmg(dmg_path: str = "", volume_name: str = ""):
    dmg_path = os.path.expanduser(dmg_path) if dmg_path else ""
    for volume in attached_volumes():
        mount_point = volume["mount_point"]
        if dmg_path and volume.get("image_path") == dmg_path:
            return Path(mount_point)
        if volume_name and Path(mount_point).name == volume_name:
            return Path(mount_point)
    return None


def find_app_in_volume(volume: Path, app_name: str = ""):
    apps = sorted(volume.glob("*.app"))
    if app_name:
        wanted = app_name if app_name.endswith(".app") else f"{app_name}.app"
        for app in apps:
            if app.name == wanted:
                return app
    return apps[0] if apps else None


def install_app_from_dmg(dmg_path: str = "~/Desktop/gc12.dmg", app_name: str = "", volume_name: str = ""):
    dmg_path = os.path.expanduser(dmg_path)
    _, attach_body = attach_dmg(dmg_path)
    volume = find_volume_for_dmg(dmg_path=dmg_path, volume_name=volume_name)
    if volume is None:
        return True, f"Could not find mounted volume for {dmg_path}.\n{attach_body}"

    app = find_app_in_volume(volume, app_name=app_name)
    if app is None:
        return True, f"No .app bundle found at {volume}."

    dest = Path("/Applications") / app.name
    if dest.exists():
        return False, f"{dest} already exists. Mounted volume: {volume}"

    rc, body = run_cmd(["ditto", str(app), str(dest)], timeout=300)
    if rc != 0:
        return True, f"ditto failed while copying {app} to {dest}.\n{body}"
    return False, f"Installed {app.name} from {volume} to {dest}.\nAttach result:\n{attach_body}"


def eject_volume(volume_name: str = "", dmg_path: str = ""):
    volume = find_volume_for_dmg(
        dmg_path=os.path.expanduser(dmg_path) if dmg_path else "",
        volume_name=volume_name,
    )
    if volume is None and volume_name:
        candidate = Path("/Volumes") / volume_name
        if candidate.exists():
            volume = candidate
    if volume is None:
        return True, "No matching mounted volume found."
    rc, body = run_cmd(["hdiutil", "detach", str(volume)], timeout=60)
    return rc != 0, body


def verify_app_installed(app_name: str):
    wanted = app_name if app_name.endswith(".app") else f"{app_name}.app"
    candidates = [Path("/Applications") / wanted, Path.home() / "Applications" / wanted]
    found = [str(p) for p in candidates if p.exists()]
    if found:
        return False, "Found installed app:\n" + "\n".join(found)
    matches = sorted(Path("/Applications").glob(f"*{app_name}*.app"))
    if matches:
        return False, "Found likely installed app:\n" + "\n".join(str(p) for p in matches)
    return True, f"{wanted} not found in /Applications or ~/Applications."


def safe_shell(command: str):
    forbidden = (" sudo ", " rm ", " rm\t", " rm\n", " mv /", " chmod -R", " chown -R", " diskutil erase")
    padded = f" {command.strip()} "
    if any(token in padded for token in forbidden):
        return True, f"Refused potentially destructive shell command: {command}"
    allowed_prefixes = ("hdiutil ", "open ", "osascript ", "ls ", "mdfind ", "mdls ", "pwd", "whoami", "defaults ")
    if not command.strip().startswith(allowed_prefixes):
        return True, f"Refused shell command outside the allow-list: {command}"
    rc, body = run_cmd(["/bin/zsh", "-lc", command], timeout=SHELL_TIMEOUT)
    return rc != 0, body
