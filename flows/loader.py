"""Load and validate a deterministic flow playbook (a `flows/*.yaml` spec).

A spec is the written-down playbook: an ordered list of sub-goals the runner drives
the agent through, plus the oracle + metadata used to score the run. Validation fails
fast and loudly — a malformed playbook must never silently "run".

`validate(dict)` is pure (no YAML dependency) so the rules are unit-testable without
PyYAML installed; `load(path)` adds the file read + `yaml.safe_load` on top.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

APP_TYPES = {"browser", "legacy", "no_api"}
MODES = {"measure", "demo"}


class SpecError(ValueError):
    """A playbook spec is missing required fields or has invalid values."""


@dataclass
class Step:
    id: str
    goal: str


@dataclass
class Spec:
    name: str
    app_type: str
    model: str
    mode: str
    oracle: str
    steps: list
    steps_expected: int
    oracle_config: dict = field(default_factory=dict)
    pattern_reference: Optional[str] = None
    emit_schema: list = field(default_factory=list)
    browser: Optional[str] = None


def validate(data: dict) -> Spec:
    """Turn a parsed spec dict into a validated Spec, or raise SpecError."""
    if not isinstance(data, dict):
        raise SpecError(f"spec must be a mapping, got {type(data).__name__}")

    for key in ("name", "app_type", "model", "mode", "steps", "oracle"):
        if key not in data or data[key] in (None, "", []):
            raise SpecError(f"spec missing required field: {key!r}")

    if data["app_type"] not in APP_TYPES:
        raise SpecError(f"app_type must be one of {sorted(APP_TYPES)}, got {data['app_type']!r}")
    if data["mode"] not in MODES:
        raise SpecError(f"mode must be one of {sorted(MODES)}, got {data['mode']!r}")

    raw_steps = data["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SpecError("steps must be a non-empty list")

    steps = []
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            raise SpecError(f"step {i} must be a mapping with id + goal")
        sid, goal = s.get("id"), s.get("goal")
        if not sid or not isinstance(sid, str):
            raise SpecError(f"step {i} missing a non-empty string id")
        if not goal or not isinstance(goal, str):
            raise SpecError(f"step {i} ({sid!r}) missing a non-empty string goal")
        steps.append(Step(id=sid, goal=goal))

    steps_expected = data.get("steps_expected", len(steps))
    if not isinstance(steps_expected, int) or steps_expected <= 0:
        raise SpecError("steps_expected must be a positive integer")

    return Spec(
        name=data["name"],
        app_type=data["app_type"],
        model=data["model"],
        mode=data["mode"],
        oracle=data["oracle"],
        steps=steps,
        steps_expected=steps_expected,
        oracle_config=data.get("oracle_config", {}) or {},
        pattern_reference=data.get("pattern_reference"),
        emit_schema=data.get("emit_schema", []) or [],
        browser=data.get("browser"),
    )


def load(path: Union[str, Path]) -> Spec:
    """Read a YAML spec file and validate it. Imports PyYAML lazily."""
    import yaml  # lazy: keeps validate() usable without the dependency

    text = Path(path).read_text()
    return validate(yaml.safe_load(text))
