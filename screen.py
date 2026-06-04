"""Screen capture and content-block helpers.

`shoot()` shells out to macOS `screencapture` (Quartz/ImageGrab hits a different
TCC permission path that fails in non-Terminal contexts) and resizes from Retina
pixels down to the point-size display that pyautogui sees, so coordinates the
model returns map 1:1 to mouse events.
"""
import base64, io, os, subprocess, tempfile

import pyautogui
from PIL import Image


TARGET_W, TARGET_H = pyautogui.size()


def shoot():
    """Capture the screen and return (PIL image at point dims, base64 PNG)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["screencapture", "-x", tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or os.path.getsize(tmp_path) == 0:
            raise RuntimeError(
                f"screencapture failed (rc={result.returncode}, size={os.path.getsize(tmp_path)}): "
                f"{result.stderr or '(no stderr; likely a Screen Recording permission issue)'}"
            )
        img = Image.open(tmp_path).convert("RGB")
        img.load()
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass

    if img.size != (TARGET_W, TARGET_H):
        img = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return img, base64.standard_b64encode(buf.getvalue()).decode()


def image_block(b64: str) -> dict:
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": b64,
    }}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_result(tool_use_id: str, content, is_error: bool = False) -> dict:
    """Build a `tool_result` content block.

    Enforces the Anthropic API rule that an errored result's content must be all
    `text` blocks: the API rejects a tool_result with ``is_error: true`` that
    also carries an image (e.g. a post-action screenshot) with
    "all content must be type `text` if `is_error` is true". So on error we keep
    only the text blocks (the screenshot is still saved to disk by the caller).
    """
    if is_error and isinstance(content, list):
        content = [b for b in content if b.get("type") == "text"] or [
            text_block("Action failed.")
        ]
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block
