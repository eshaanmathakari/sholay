"""Per-run artifact recorder: numbered screenshots + markdown transcript."""
from datetime import datetime
from pathlib import Path

from PIL import Image

from screen import TARGET_W, TARGET_H


class Recorder:
    def __init__(self, root="runs"):
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.dir = Path(root) / ts
        self.dir.mkdir(parents=True, exist_ok=True)
        self.step = 0
        self.log = (self.dir / "transcript.md").open("w")
        self.log.write(f"# Run {ts}\n\nDisplay: {TARGET_W}x{TARGET_H}\n\n")

    def save_screenshot(self, img: Image.Image, label: str):
        self.step += 1
        path = self.dir / f"{self.step:03d}_{label}.png"
        img.save(path)
        return path.name

    def heading(self, text: str):
        self.log.write(f"## Step {self.step}: {text}\n\n")
        self.log.flush()

    def write(self, text: str):
        self.log.write(f"{text}\n\n")
        self.log.flush()

    def close(self):
        self.log.close()
