"""Ask the session metadata fields defined in config/session_fields.toml."""
from __future__ import annotations

import json
from typing import Callable

AskFn = Callable[[str, str | None, list | None], str]


def load_state(path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def rich_ask(prompt: str, default: str | None, choices: list | None) -> str:
    from rich.prompt import Prompt

    return Prompt.ask(prompt, default=default, choices=choices, show_choices=bool(choices))


def _convert(field: dict, raw: str):
    t = field.get("type", "str")
    if raw is None or raw == "":
        return None
    if t == "int":
        return int(raw)
    if t == "float":
        return float(raw)
    if t == "bool":
        return str(raw).strip().lower() in ("y", "yes", "true", "1")
    return str(raw)


def ask_fields(fields: list[dict], phase: str, catalog, state: dict, preset: dict,
               ask: AskFn = rich_ask, non_interactive: bool = False, context: dict | None = None) -> dict:
    """Return {name: value} for fields asked in `phase` ("before"/"after").

    Values in `preset` (from --meta key=value) are used without asking.
    `context` holds answers from earlier phases (needed for trial auto-numbering).
    """
    out: dict = {}
    ctx = {**(context or {})}
    last = state.get("last", {})
    for f in fields:
        if f.get("ask", "before") != phase:
            continue
        name = f["name"]
        if name in preset:
            out[name] = _convert(f, preset[name])
            ctx[name] = out[name]
            continue
        default = f.get("default")
        if f.get("remember") and name in last:
            default = last[name]
        if default == "auto" and name == "trial":
            default = catalog.next_trial(ctx.get("subject_id", ""), ctx.get("protocol", ""))
        if f.get("type") == "bool" and isinstance(default, bool):
            default = "y" if default else "n"
        choices = f.get("choices") if f.get("type") == "choice" else None
        if f.get("type") == "bool":
            choices = ["y", "n"]
        if f.get("type") == "subject" and not default:
            ids = [s["subject_id"] for s in catalog.subjects()]
            default = ids[-1] if ids else catalog.next_subject_id()
        default = None if default is None else str(default)

        while True:
            if non_interactive:
                raw = default
            else:
                raw = ask(f.get("prompt", name), default, choices)
            try:
                val = _convert(f, raw)
            except ValueError:
                if non_interactive:
                    raise
                print(f"  ! {name}: expected {f.get('type')}")
                continue
            if val is None and f.get("required", True) and f.get("type") not in ("bool",):
                if non_interactive:
                    raise ValueError(f"required field {name!r} has no default; pass --meta {name}=...")
                print(f"  ! {name} is required")
                continue
            break
        out[name] = val
        ctx[name] = val
    return out


SUBJECT_FIELDS = [
    {"name": "sex", "prompt": "  Sex", "type": "choice", "choices": ["F", "M", "other", "na"]},
    {"name": "age_years", "prompt": "  Age (years)", "type": "float", "required": False},
    {"name": "height_cm", "prompt": "  Height (cm)", "type": "float", "required": False},
    {"name": "weight_kg", "prompt": "  Weight (kg)", "type": "float", "required": False},
    {"name": "fitzpatrick", "prompt": "  Skin type (Fitzpatrick I-VI)", "type": "choice",
     "choices": ["I", "II", "III", "IV", "V", "VI", "na"]},
    {"name": "notes", "prompt": "  Notes", "type": "str", "required": False},
]


def ask_subject(subject_id: str, catalog, ask: AskFn = rich_ask, non_interactive: bool = False) -> dict:
    d = {"subject_id": subject_id}
    for f in SUBJECT_FIELDS:
        if non_interactive:
            d[f["name"]] = None
            continue
        while True:
            raw = ask(f["prompt"], None if f.get("required", True) is False else "na" if f.get("choices") else None,
                      f.get("choices"))
            try:
                d[f["name"]] = _convert(f, raw)
                break
            except ValueError:
                print("  ! expected a number")
    catalog.upsert_subject(d)
    return d
