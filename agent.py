"""
agent.py - the pre-screen agent: Plan -> Fetch -> Analyze -> Report.

The agent is domain-agnostic. Everything domain-specific lives in a YAML
"check pack" in packs/ (hiring, business partner, vendor, ...):
  - which inputs to ask for
  - which checks to run, and with which inputs
  - which red flags matter in that domain
  - what we cannot check (shown in the "Not checked" section)

  1. Plan   : load the pack, fill each check's params from the user's inputs
              (and from earlier checks), skip checks with missing inputs
  2. Fetch  : run each check (one tool call each) -> facts + sources
  3. Analyze: one LLM call reads ONLY the fetched evidence and lists red flags.
              Each flag must cite source ids; flags without a valid source are dropped.
  4. Report : risk level + recommendation are computed in plain code from the flags.

Golden rule: the LLM only sees data we fetched. It never answers from memory.
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

import checks
import config
import tools

MODEL = "gemini-3.5-flash-lite"
PACKS_DIR = Path(__file__).parent / "packs"


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------

def list_packs() -> dict:
    """{pack_name: pack_dict} for every YAML file in packs/."""
    packs = {}
    for path in sorted(PACKS_DIR.glob("*.yaml")):
        pack = yaml.safe_load(path.read_text(encoding="utf-8"))
        packs[pack["name"]] = pack
    return packs


def load_pack(name: str) -> dict:
    return list_packs()[name]


# ---------------------------------------------------------------------------
# Step 1: Plan  (fill "{placeholders}" in check params)
# ---------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def fill(template: str, values: dict) -> str:
    """
    "{name}" -> values["name"].
    "{company|person.current_company}" -> first non-empty of the two.
    Returns "" if any placeholder in the template has no value.
    """
    missing = False

    def sub(match):
        nonlocal missing
        for key in match.group(1).split("|"):
            v = values.get(key.strip())
            if v not in (None, ""):
                return str(v)
        missing = True
        return ""

    out = PLACEHOLDER.sub(sub, template)
    return "" if missing else out.strip()


def resolve_params(params: dict, values: dict) -> dict:
    """Fill every param. Lists (like queries) drop items with missing values."""
    resolved = {}
    for key, raw in (params or {}).items():
        if isinstance(raw, list):
            resolved[key] = [s for s in (fill(x, values) if isinstance(x, str) else x
                                         for x in raw) if s]
        elif isinstance(raw, str):
            resolved[key] = fill(raw, values)
        else:
            resolved[key] = raw
    return resolved


# ---------------------------------------------------------------------------
# Step 2: Fetch
# ---------------------------------------------------------------------------

def run_checks(pack: dict, inputs: dict) -> tuple[list, list]:
    """Run the pack's checks in order. Returns (check_results, not_checked)."""
    values = {k: v for k, v in inputs.items() if v not in (None, "")}
    results, not_checked = [], []

    for step in pack["checks"]:
        spec = checks.CHECKS.get(step["tool"])
        label = step.get("label") or (spec["label"] if spec else step["tool"])
        if spec is None:
            not_checked.append({"item": label, "reason": f"Unknown tool '{step['tool']}'"})
            continue

        params = resolve_params(step.get("params"), values)
        if not any(params.get(k) for k in spec["needs_any"]):
            not_checked.append({"item": label, "reason": "Required input not provided"})
            continue

        ev = spec["fn"](**params)
        ev.update({"id": step["id"], "label": label, "tool": step["tool"], "params": params})
        results.append(ev)

        if ev["status"] == "error":
            not_checked.append({"item": label, "reason": f"Data source error: {ev['error']}"})
        # Let later checks use what this one found (e.g. the person's current company).
        values.update({k: v for k, v in ev["context"].items() if v})

    # Give every source a short id (S1, S2, ...) that the LLM can cite.
    n = 0
    for ev in results:
        for src in ev["sources"]:
            n += 1
            src["id"] = f"S{n}"
    return results, not_checked


# ---------------------------------------------------------------------------
# Step 3: Analyze  (one LLM call, strict JSON)
# ---------------------------------------------------------------------------

class RedFlag(BaseModel):
    title: str                       # short, e.g. "Headcount down 60% from peak"
    severity: Literal["low", "medium", "high"]
    rule_id: str                     # id from the pack's red_flags list, or "other"
    detail: str                      # 1-2 sentences: what the evidence shows and why it matters
    source_ids: list[str]            # e.g. ["S3", "S5"]; must come from the evidence


class Positive(BaseModel):
    detail: str
    source_ids: list[str]


class Analysis(BaseModel):
    identity: str                    # who we believe the subject is, in one sentence
    identity_confidence: Literal["high", "medium", "low", "not_found"]
    red_flags: list[RedFlag]
    positives: list[Positive]
    summary: str                     # 2-3 neutral sentences


SYSTEM_PROMPT = """You are a careful due-diligence analyst doing a fast pre-screen before a decision
(hiring someone, a business deal, choosing a vendor, ...). Your job: identify the subject and point
out RED FLAGS the decision-maker should consider, using ONLY the evidence provided.

Rules:
- Use only the EVIDENCE. Never use outside knowledge, never guess. If unsure, leave it out.
- Every red flag and positive must cite source_ids (like "S3") taken from the evidence.
- Web/news results may be about a different person or company with a similar name.
  Only use a result if its title/snippet clearly refers to this subject.
- Use the pack's red-flag list as your checklist (rule_id = its id). Flag something else
  only if it clearly matters for this decision (rule_id = "other").
- Severity: high = could stop the deal on its own (fraud, court case, insolvency, fake identity);
  medium = needs a clarifying conversation; low = minor, worth knowing.
- Missing data is NOT a red flag by itself. Say what was found, neutrally.
- News about a person's employer is NOT about the person, unless it names their own
  involvement. Do not flag a person for their company's news (and vice versa).
  If your own detail would say "this is not about the subject", do not output that flag.
- If no profile matched the subject, identity_confidence is "not_found".
- CLAIMS: if subject_inputs has "claims", split them into individual facts (company, title,
  start year, duration, size...) and compare EACH one to the evidence. Any contradiction,
  even a few years' difference, is a red flag with rule_id "claim_mismatch" and severity at
  least "medium" (the subject told you something the record does not support)."""


def _llm_payload(pack: dict, inputs: dict, results: list) -> dict:
    return {
        "decision_context": pack["label"],
        "purpose": pack.get("description", ""),
        "red_flag_checklist": pack.get("red_flags", []),
        "subject_inputs": inputs,
        "evidence": [
            {"check": ev["label"], "status": ev["status"], "facts": ev["facts"],
             "sources": [{k: s.get(k) for k in ("id", "title", "snippet")} for s in ev["sources"]]}
            for ev in results
        ],
    }


def analyze(payload: dict) -> dict:
    """Ask Gemini for red flags. Cached to disk like the API calls."""
    key = tools.cache_key("llm", {"model": MODEL, "prompt": SYSTEM_PROMPT, "payload": payload})
    cached = tools.read_cache(key)
    if cached:
        return {"ok": True, "analysis": cached, "from_cache": True}
    if not config.GEMINI_API_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY is empty. Add it to config.py."}

    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response, last_error = None, None
    for attempt in range(3):   # Gemini sometimes returns 503 "high demand": wait and retry
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents="EVIDENCE PACKAGE:\n" + json.dumps(payload, indent=1, ensure_ascii=False),
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "response_mime_type": "application/json",  # strict JSON out
                    "response_schema": Analysis,               # validated against the Pydantic model
                    "temperature": 0,                          # same input -> same answer
                },
            )
            break
        except Exception as e:  # network, rate limit, overload... retry, then show it
            last_error = e
            time.sleep(3 * (attempt + 1))
    if response is None:
        return {"ok": False, "error": f"LLM call failed: {type(last_error).__name__}: {last_error}"}

    if response.parsed is None:
        return {"ok": False, "error": f"LLM gave no usable answer: {(response.text or '')[:300]}"}
    analysis = response.parsed.model_dump()
    tools.write_cache(key, analysis)
    return {"ok": True, "analysis": analysis, "from_cache": False}


# ---------------------------------------------------------------------------
# Step 4: Report  (risk + recommendation in plain code: predictable and explainable)
# ---------------------------------------------------------------------------

RECOMMENDATIONS = {
    "Low": "Looks fine. Proceed with your normal process.",
    "Medium": "Clarify the flagged points with the subject before proceeding.",
    "High": "Order a full paid background check before proceeding.",
}


def score(red_flags: list, identity_confidence: str) -> str:
    severities = [f["severity"] for f in red_flags]
    if "high" in severities:
        return "High"
    if "medium" in severities or identity_confidence in ("low", "not_found"):
        return "Medium"
    return "Low"


def _attach_sources(items: list, by_id: dict) -> list:
    """Replace source ids with real links. Drop items that cite no real source."""
    kept = []
    for item in items:
        srcs = [by_id[i] for i in item.get("source_ids", []) if i in by_id]
        if srcs:
            kept.append({**item, "sources": [{"id": s["id"], "title": s["title"], "url": s["url"]}
                                             for s in srcs]})
    return kept


def run(pack_name: str, inputs: dict) -> dict:
    """The whole loop. Returns one dict the UI (or CLI) can render."""
    started = time.time()
    pack = load_pack(pack_name)
    results, not_checked = run_checks(pack, inputs)
    not_checked += [{"item": item, "reason": "Not available in public professional data"}
                    for item in pack.get("not_checked", [])]

    report = {"pack": pack["name"], "pack_label": pack["label"], "inputs": inputs,
              "checks": results, "not_checked": not_checked}

    analyzed = analyze(_llm_payload(pack, inputs, results)) if results else \
        {"ok": False, "error": "No checks could run. Provide more inputs."}
    if not analyzed["ok"]:
        report.update(status="error", error=analyzed["error"])
    else:
        a = analyzed["analysis"]
        by_id = {s["id"]: s for ev in results for s in ev["sources"]}
        flags = _attach_sources(a["red_flags"], by_id)
        order = {"high": 0, "medium": 1, "low": 2}
        flags.sort(key=lambda f: order[f["severity"]])
        risk = score(flags, a["identity_confidence"])
        report.update(
            status="done",
            identity=a["identity"], identity_confidence=a["identity_confidence"],
            red_flags=flags,
            dropped_flags=len(a["red_flags"]) - len(flags),   # flags with no valid source
            positives=_attach_sources(a["positives"], by_id),
            summary=a["summary"], risk_level=risk, recommendation=RECOMMENDATIONS[risk],
            llm_from_cache=analyzed["from_cache"])

    credits = [float(ev["credits"]) for ev in results if ev.get("credits")]
    report["stats"] = {"seconds": round(time.time() - started, 1),
                       "credits_used": round(sum(credits), 2),
                       "checks_run": len(results)}
    return report


if __name__ == "__main__":
    # `python agent.py [pack]` -> run that pack's demo case and print the report.
    name = sys.argv[1] if len(sys.argv) > 1 else "hiring"
    print(json.dumps(run(name, load_pack(name)["demo"]), indent=2, ensure_ascii=False))
