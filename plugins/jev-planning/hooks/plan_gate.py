"""Jev plan gate: a Claude Code PreToolUse hook on ExitPlanMode.

Reads the plan from the hook payload and the original request from the
session transcript, asks TypeSafe's Jev one batch of typed questions, applies
the thresholds in rubric.json, and denies the plan-mode exit with the failing
items when the plan falls short. Denies are capped per session so a plan can
never loop forever; a missing key or any Jev failure lets the exit through,
because an outage must never block planning. On a pass the hook prints
nothing, so the operator's normal plan-approval prompt is unchanged.

Also runnable as ``plan_gate.py --dry-run <plan.md> ["<request>"]`` to print
the state, every answer, and the verdict without the hook protocol.
"""

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
HERE = Path(__file__).resolve().parent
MAX_REQUIREMENTS = 20  # ponytail: hard cap on request sentences; rank by relevance if requests outgrow it
MAX_TEXT = 2000  # ponytail: per-candidate/step truncation keeps state far below Jev's 32k-token budget
SHAPE_HELP = (
    "Jev plan gate: the plan needs two sections before it can be scored.\n"
    "## Candidates — 2 to 3 approaches, each as `### <name>` with what changes, files touched, "
    "new-code size, and risk; then one line `Chosen: <name>`.\n"
    "## Plan — numbered steps, each naming its check (a test, a command, or an observable result)."
)


class JevUnavailable(Exception):
    """Jev could not answer: no key, network failure, or a non-retryable HTTP error."""


def section(plan, title):
    """Text under ``## <title>`` up to the next ``## `` heading, or an empty string."""
    m = re.search(rf"^##\s+{re.escape(title)}\b[^\n]*\n(.*?)(?=^##\s|\Z)", plan, re.M | re.S)
    return m.group(1) if m else ""


def parse_candidates(plan):
    out = []
    for m in re.finditer(r"^###\s+(.+?)\s*$\n(.*?)(?=^###\s|\Z)", section(plan, "Candidates"), re.M | re.S):
        name = re.sub(r"\s*\((chosen|recommended)\)\s*$", "", m.group(1), flags=re.I)
        out.append({"name": name.strip("*_` "), "text": m.group(2).strip()[:MAX_TEXT]})
    return out


def parse_steps(plan):
    steps = []
    for line in section(plan, "Plan").splitlines():
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)", line)
        if m:
            steps.append(f"{m.group(1)}. {m.group(2).strip()}")
        elif steps and line.strip() and not line.lstrip().startswith("#"):
            steps[-1] = (steps[-1] + " " + line.strip())[:MAX_TEXT]
    return steps


def parse_marker(plan, label):
    """``<label> [name]: value`` lines as (name, value), tolerating markdown bold on either side."""
    pattern = rf"^[\s*_]*{label}\s*(.*?)[\s*_]*:[\s*_]*(.+?)\s*$"
    return [(m.group(1).strip("*_` "), m.group(2).strip("*_ ")) for m in re.finditer(pattern, plan, re.M | re.I)]


def split_requirements(request):
    pieces = re.split(r"(?<=[.!?])\s+|\n+", request or "")
    return [p.strip(" -*") for p in pieces if len(p.split()) >= 4][:MAX_REQUIREMENTS]


def parse_plan(plan, request):
    chosen = parse_marker(plan, "Chosen")
    return {
        "request": request or "",
        "requirements": split_requirements(request),
        "candidates": parse_candidates(plan),
        "chosen": chosen[0][1] if chosen else "",
        "why_not": dict(parse_marker(plan, "Why not")),
        "steps": parse_steps(plan),
    }


def shape_problems(state):
    missing = []
    if len(state["candidates"]) < 2:
        missing.append("a `## Candidates` section with 2 to 3 `### <name>` approaches")
    if not state["chosen"]:
        missing.append("a `Chosen: <name>` line")
    if not state["steps"]:
        missing.append("a `## Plan` section with numbered steps")
    return missing


def original_request(transcript_path):
    """The first substantive prompt the user typed this session, or None."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, str) and not content.lstrip().startswith("<") and len(content.strip()) >= 20:
                    return content.strip()
    except OSError:
        return None
    return None


def first_paragraph(plan):
    for para in re.split(r"\n\s*\n", plan):
        if para.strip() and not para.lstrip().startswith("#"):
            return para.strip()
    return plan.strip()[:500]


def load_rubric():
    return json.loads((HERE / "rubric.json").read_text(encoding="utf-8"))


def load_extras(cwd):
    path = Path(cwd) / ".claude" / "jev-rubric.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def build_questions(state, rubric, extras):
    """One question per candidate, requirement, and step dimension, all over the same state."""
    templates = rubric["questions"]

    def fill(key, **index):
        q = dict(templates[key])
        q["instructions"] = q["instructions"].format(**index)
        return q

    out = {}
    names = [c["name"] for c in state["candidates"]]
    if len(names) >= 2:
        out["best_candidate"] = {**templates["best_candidate"], "criteria": {n: None for n in names}}
    for i in range(len(names)):
        for key in ("cand_solves", "cand_size", "cand_risk"):
            out[f"{key}_{i}"] = fill(key, i=i)
    for j in range(len(state["requirements"])):
        out[f"req_covered_{j}"] = fill("req_covered", j=j)
    out["scope_creep"] = fill("scope_creep")
    out["unsafe_without_guard"] = fill("unsafe_without_guard")
    for k in range(len(state["steps"])):
        for key in ("step_needed", "step_checked", "step_altitude"):
            out[f"{key}_{k}"] = fill(key, k=k)
    for extra in extras:
        q = {"type": "noul", "instructions": extra["instructions"]}
        if extra.get("criteria"):
            q["criteria"] = extra["criteria"]
        out[f"extra_{extra['id']}"] = q
    return out


def ask_jev(state, questions, model):
    """One POST carrying every question; retries 429/529 twice, fails fast on anything else.

    The key arrives as ``CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY`` when the plugin's enable-time
    prompt supplied it, or as ``TYPESAFE_API_KEY`` when the hook is wired by path.
    """
    key = os.environ.get("CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise JevUnavailable("no API key: set TYPESAFE_API_KEY, or enter it when enabling the plugin")
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(API_URL, body, headers), timeout=30) as resp:
                return json.load(resp)["answers"]
        except urllib.error.HTTPError as err:
            if err.code not in (429, 529) or attempt == 2:
                raise JevUnavailable(f"HTTP {err.code}: {err.read(300).decode('utf-8', 'replace')}") from err
        except OSError as err:
            if attempt == 2:
                raise JevUnavailable(str(err)) from err
        time.sleep(1 + 2 * attempt)


def short(text, limit=160):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evaluate(state, answers, rubric, extras):
    """Apply the rubric thresholds; return (blocking findings, advisory findings)."""
    t = rubric["thresholds"]
    blocks, warns = [], []
    reqs, steps = state["requirements"], state["steps"]

    def noul(key):
        return answers[key]["noul"]

    def listing(header, items):
        return header + "\n" + "\n".join(f"  - {item}" for item in items)

    bad = [f'"{reqs[j]}" ({noul(f"req_covered_{j}"):.2f})' for j in range(len(reqs)) if noul(f"req_covered_{j}") < t["covered_min"]]
    if bad:
        blocks.append(listing("Not covered by any step:", bad))
    if noul("scope_creep") > t["scope_creep_max"]:
        blocks.append(f"Steps do work the request did not ask for ({noul('scope_creep'):.2f}). Drop them, or restate the request.")
    if noul("unsafe_without_guard") > t["unsafe_max"]:
        blocks.append(f"A step changes prod config, a validation boundary, or a tasking gate with no verifying step ({noul('unsafe_without_guard'):.2f}).")
    bad = [f'{short(steps[k])} ({noul(f"step_needed_{k}"):.2f})' for k in range(len(steps)) if noul(f"step_needed_{k}") < t["needed_min"]]
    if bad:
        blocks.append(listing("Steps not needed for the request (drop, or restate the request):", bad))
    bad = [f'{short(steps[k])} ({noul(f"step_checked_{k}"):.2f})' for k in range(len(steps)) if noul(f"step_checked_{k}") < t["checked_min"]]
    if bad:
        blocks.append(listing("Steps with no check named:", bad))
    best = answers.get("best_candidate")
    if (best and best["choice"].lower() != state["chosen"].lower() and best["confidence"] >= t["mismatch_confidence_min"]
            and best["choice"].lower() not in {n.lower() for n in state["why_not"]}):
        blocks.append(f'Jev prefers candidate "{best["choice"]}" over "{state["chosen"]}" ({best["probabilities"][best["choice"]]:.2f}). '
                      f'Switch, or add a line "Why not {best["choice"]}: <reason>".')
    for k in range(len(steps)):
        a = answers[f"step_altitude_{k}"]
        if a["score"] >= t["altitude_warn_min"]:
            level = a["legend"].get(str(round(a["score"])), "").lower()
            warns.append(f'{short(steps[k])} — {level} (altitude {a["score"]:.1f}). A smaller change may do.')
    for extra in extras:
        p = noul(f"extra_{extra['id']}")
        if p > extra["above"]:
            (blocks if extra.get("action") == "block" else warns).append(f'{extra.get("message", extra["instructions"])} ({p:.2f})')
    return blocks, warns


def gate(plan, request, cwd, rubric, ask=None):
    """Verdict for one plan as (blocks, warns); raises JevUnavailable."""
    state = parse_plan(plan, request)
    missing = shape_problems(state)
    if missing:
        return [SHAPE_HELP + "\nMissing: " + "; ".join(missing) + "."], []
    extras = load_extras(cwd)
    answers = (ask or ask_jev)(state, build_questions(state, rubric, extras), rubric["model"])
    return evaluate(state, answers, rubric, extras)


def counter_file(session_id):
    folder = Path(tempfile.gettempdir()) / "jev-plan-gate"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{session_id or 'nosession'}.count"


def bump_bounces(session_id):
    f = counter_file(session_id)
    n = (int(f.read_text() or 0) if f.exists() else 0) + 1
    f.write_text(str(n))
    return n


def reset_bounces(session_id):
    f = counter_file(session_id)
    if f.exists():
        f.unlink()


def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}))


def run_hook(payload, rubric=None, ask=None):
    if payload.get("tool_name") != "ExitPlanMode":
        return 0
    rubric = rubric or load_rubric()
    plan = (payload.get("tool_input") or {}).get("plan") or ""
    request = original_request(payload.get("transcript_path")) or first_paragraph(plan)
    session = payload.get("session_id", "")
    try:
        blocks, warns = gate(plan, request, payload.get("cwd") or ".", rubric, ask)
    except JevUnavailable as err:
        print(f"Jev plan gate: skipped ({err})", file=sys.stderr)
        return 0
    if not blocks:
        reset_bounces(session)
        for w in warns:
            print(f"Jev plan gate advisory: {w}", file=sys.stderr)
        return 0
    n = bump_bounces(session)
    findings = "\n".join(blocks + [f"Advisory: {w}" for w in warns])
    if n > rubric["max_bounces"]:
        reset_bounces(session)
        emit("ask", f"Jev plan gate: {n - 1} bounces used; passing this plan to you with its open findings.\n{findings}")
        return 0
    emit("deny", f"Jev plan gate — bounce {n} of {rubric['max_bounces']}. Fix these and exit plan mode again.\n{findings}")
    return 0


def dry_run(plan_path, request):
    rubric = load_rubric()
    plan = Path(plan_path).read_text(encoding="utf-8")
    state = parse_plan(plan, request or first_paragraph(plan))
    print(json.dumps(state, indent=2)[:4000])
    missing = shape_problems(state)
    if missing:
        print("SHAPE:", "; ".join(missing))
        return 1
    extras = load_extras(os.getcwd())
    answers = ask_jev(state, build_questions(state, rubric, extras), rubric["model"])
    for key, a in answers.items():
        value = a.get("choice") if a["type"] == "choice" else f'{a.get("noul", a.get("score")):.2f}'
        conf = f' conf={a["confidence"]:.2f}' if "confidence" in a else ""
        print(f"{key:28} {value}{conf}")
    blocks, warns = evaluate(state, answers, rubric, extras)
    print("\nBLOCK" if blocks else "\nPASS")
    print("\n".join(blocks + [f"warn: {w}" for w in warns]))
    return 1 if blocks else 0


def main(argv):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(argv) >= 3 and argv[1] == "--dry-run":
        return dry_run(argv[2], argv[3] if len(argv) > 3 else None)
    try:
        return run_hook(json.load(sys.stdin))
    except Exception as err:  # a bug in the gate must never block planning
        print(f"Jev plan gate: skipped ({type(err).__name__}: {err})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
