"""
agent.py - the pre-screen agent: Plan -> Fetch -> Analyze -> Report.

The agent is domain-agnostic. Everything domain-specific lives in a YAML
"check pack" in packs/ (hiring, business partner, vendor, ...):
  - which inputs to ask for
  - which checks to run, with which inputs, at which tier
  - which red flags matter in that domain, and how much (their tier)
  - what we cannot check (shown in the "Not checked" section)

  1. Plan   : load the pack, order checks by tier (Tier 1 deal-breakers first),
              fill each check's params from the inputs and from earlier checks
  2. Fetch  : run each check -> facts + sources (each with a reliability level)
              - the "gate" check confirms identity; a weak identity is flagged
              - a deal-breaker (e.g. sanctions match) skips the remaining Tier 2+ checks
  3. Analyze: one LLM call reads ONLY the fetched evidence and lists red flags.
              Each flag must cite source ids; flags without a valid source are dropped.
  4. Report : each flag is scored = severity x rule tier x source reliability,
              so a weak source never outranks an official record. Risk level and
              recommendation are computed in plain code from those scores.

Golden rule: the LLM only sees data we fetched. It never answers from memory.
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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

def _merge_by(base_items: list, pack_items: list, key: str) -> list:
    """Merge two lists of dicts by `key`: same key -> pack's fields win;
    new keys are appended; {key: x, skip: true} removes x."""
    merged = {item[key]: dict(item) for item in base_items}
    for item in pack_items:
        if item.get("skip"):
            merged.pop(item[key], None)
        elif item[key] in merged:
            merged[item[key]].update(item)
        else:
            merged[item[key]] = dict(item)
    return list(merged.values())


def merge_pack(base: dict, pack: dict) -> dict:
    """Domain pack on top of a base pack (the checks common to every person/company)."""
    out = {**base, **{k: v for k, v in pack.items()
                      if k not in ("inputs", "checks", "red_flags", "not_checked")}}
    out["inputs"] = _merge_by(base.get("inputs", []), pack.get("inputs", []), "key")
    out["checks"] = _merge_by(base.get("checks", []), pack.get("checks", []), "id")
    out["red_flags"] = _merge_by(base.get("red_flags", []), pack.get("red_flags", []), "id")
    out["not_checked"] = list(dict.fromkeys(base.get("not_checked", []) + pack.get("not_checked", [])))
    return out


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_packs() -> dict:
    """{pack_name: pack_dict} for every YAML file in packs/, with its bases merged in.
    Bases live in packs/base/ and are never shown in the dropdown."""
    bases = {p.stem: _read_yaml(p) for p in sorted((PACKS_DIR / "base").glob("*.yaml"))}
    packs = {}
    for path in sorted(PACKS_DIR.glob("*.yaml")):
        pack = _read_yaml(path)
        extends = pack.get("extends") or []
        merged = {}
        for base_name in ([extends] if isinstance(extends, str) else extends):
            merged = merge_pack(merged, bases[base_name])
        packs[pack["name"]] = merge_pack(merged, pack)
    return packs


def load_pack(name: str) -> dict:
    return list_packs()[name]


# ---------------------------------------------------------------------------
# Step 1: Plan  (fill "{placeholders}" in check params, order by tier)
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


def _fill_any(x, values):
    """Fill a string, or every string inside a dict (e.g. {"q": ..., "site": ...})."""
    if isinstance(x, str):
        return fill(x, values)
    if isinstance(x, dict):
        filled = {k: _fill_any(v, values) for k, v in x.items()}
        return filled if filled.get("q", "x") else None   # drop queries with missing values
    return x


def resolve_params(params: dict, values: dict) -> dict:
    """Fill every param. Lists (queries, names) drop items with missing values."""
    resolved = {}
    for key, raw in (params or {}).items():
        if isinstance(raw, list):
            resolved[key] = [y for y in (_fill_any(x, values) for x in raw) if y]
        else:
            resolved[key] = _fill_any(raw, values)
    return resolved


def plan(pack: dict) -> list:
    """Each step gets its tier (pack override, else registry default). Tier 1 runs first;
    within a tier the identity gate runs first, then the pack's order is kept."""
    steps = []
    for i, step in enumerate(pack["checks"]):
        spec = checks.CHECKS.get(step["tool"], {})
        steps.append({**step, "tier": step.get("tier", spec.get("tier", 2)), "_order": i})
    return sorted(steps, key=lambda s: (s["tier"], not s.get("gate"), s["_order"]))


# ---------------------------------------------------------------------------
# Step 2: Fetch
# ---------------------------------------------------------------------------

# Checks whose results later checks reuse ({person.name}, {company.domain}...).
# They run one at a time, in order; all other checks of the same tier run in parallel.
PROFILE_TOOLS = {"person_profile", "company_profile", "vehicle_lookup", "nonprofit_filings"}
PARALLEL_WORKERS = 4   # stays under Crustdata web search's 10 requests/minute


def run_checks(pack: dict, inputs: dict) -> dict:
    """Run the planned checks. Returns results, not_checked, identity and deal_breaker."""
    values = {k: v for k, v in inputs.items() if v not in (None, "")}
    results, not_checked, skipped = [], [], []
    state = {"identity": None, "deal_breaker": None}

    def prepare(step):
        """Fill params, or return None (and note why) if the check can't run."""
        spec = checks.CHECKS.get(step["tool"])
        label = step.get("label") or (spec["label"] if spec else step["tool"])
        if spec is None:
            not_checked.append({"item": label, "reason": f"Unknown tool '{step['tool']}'"})
            return None
        # Short-circuit: after a deal-breaker, skip the rest of the lower-tier checks.
        if state["deal_breaker"] and step["tier"] > 1:
            not_checked.append({"item": label, "reason": "Skipped: a Tier 1 deal-breaker was found"})
            return None
        params = resolve_params(step.get("params"), values)
        if not any(params.get(k) for k in spec["needs_any"]):
            skipped.append(label)          # an optional input was left blank: not a gap
            return None
        return spec, label, params

    def record(step, label, params, ev):
        """Store one check's evidence and what it tells later checks."""
        ev.update({"id": step["id"], "label": label, "tool": step["tool"],
                   "tier": step["tier"], "params": params, "_order": step["_order"]})
        results.append(ev)
        if ev["status"] == "error":
            not_checked.append({"item": label, "reason": f"Data source error: {ev['error']}"})
        if step.get("gate") and ev.get("identity") and state["identity"] is None:
            state["identity"] = ev["identity"]     # the identity gate (first gate that ran wins)
        if ev.get("deal_breaker") and not state["deal_breaker"]:
            state["deal_breaker"] = {"check": label, "reason": ev["deal_breaker"]}
        # Let later checks use what this one found (e.g. the person's current company)...
        # ...but only if we're reasonably sure it's the right entity. Otherwise later checks
        # would search for a namesake; they fall back to what the user typed instead.
        if (ev.get("identity") or {}).get("confidence") in ("low", "not_found"):
            return
        values.update({k: v for k, v in ev["context"].items() if v})

    steps = plan(pack)
    for tier in sorted({s["tier"] for s in steps}):
        in_tier = [s for s in steps if s["tier"] == tier]
        # 1) Gate and profile checks, one by one (later checks need their results).
        for step in [s for s in in_tier if s.get("gate") or s["tool"] in PROFILE_TOOLS]:
            prepared = prepare(step)
            if prepared:
                spec, label, params = prepared
                record(step, label, params, spec["fn"](**params))
        # 2) Everything else in this tier (searches, screening) in parallel.
        jobs = []
        for step in [s for s in in_tier if not (s.get("gate") or s["tool"] in PROFILE_TOOLS)]:
            prepared = prepare(step)
            if prepared:
                jobs.append((step, *prepared))
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            futures = [(step, label, params, pool.submit(spec["fn"], **params))
                       for step, spec, label, params in jobs]
            for step, label, params, future in futures:
                record(step, label, params, future.result())

    # Keep the report in plan order (tier, gate first, pack order).
    order = {s["_order"]: i for i, s in enumerate(steps)}
    results.sort(key=lambda ev: order[ev["_order"]])
    identity, deal_breaker = state["identity"], state["deal_breaker"]

    # Give every source a short id (S1, S2, ...) that the LLM can cite.
    n = 0
    for ev in results:
        for src in ev["sources"]:
            n += 1
            src["id"] = f"S{n}"
    return {"results": results, "not_checked": not_checked, "skipped": skipped,
            "identity": identity, "deal_breaker": deal_breaker}


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
    red_flags: list[RedFlag]
    positives: list[Positive]
    summary: str                     # 2-3 neutral sentences


SYSTEM_PROMPT = """You are a careful due-diligence analyst doing a fast pre-screen before a decision
(hiring someone, a business deal, choosing a vendor, ...). Your job: identify the subject and point
out RED FLAGS the decision-maker should consider, using ONLY the evidence provided.

Rules:
- Use only the EVIDENCE. Never use outside knowledge, never guess. If unsure, leave it out.
- Every red flag and positive must cite source_ids (like "S3") taken from the evidence.
- Each source has a reliability: official (government list, court, regulator) > record (data
  provider) > news > web. Prefer official and record sources; a blog or review alone is weak.
- Web/news/court results may be about a different person or company with a similar name.
  Only use a result if its title/snippet clearly refers to this subject. Court and regulator
  hits for common names need extra care: if it is not clearly this subject, leave it out.
- News about a person's employer is NOT about the person, unless it names their own
  involvement. Do not flag a person for their company's news (and vice versa).
  If your own detail would say "this is not about the subject", do not output that flag.
- A sanctions "strong" match is a high-severity flag (rule_id "sanctions"); a "possible" match
  is medium. Always add that a name match must be confirmed with other identifiers.
  "no match" is NOT a flag: put a clean sanctions screen under positives.
- Use the pack's red-flag checklist (rule_id = its id). Flag something else only if it
  clearly matters for this decision (rule_id = "other").
- Severity: high = could stop the deal on its own (sanctions, fraud, court case, insolvency,
  fake identity); medium = needs a clarifying conversation; low = minor, worth knowing.
- Missing data is NOT a red flag by itself. If the identity_gate says identity is low or
  not_found, mention it in the summary instead of guessing.
- If identity_gate is low or not_found, the matched profile/record may be a DIFFERENT
  person or company (a namesake). Do NOT compare claims against it and do NOT flag
  anything based on it. Base flags on searches for the typed name instead.
- CLAIMS: if subject_inputs has "claims", split them into individual facts (company, title,
  start year, duration, degree, size...) and compare EACH one to the evidence. Any
  contradiction is a red flag with rule_id "claim_mismatch" and severity at least "medium"."""


def _llm_payload(pack: dict, inputs: dict, fetched: dict) -> dict:
    return {
        "decision_context": pack["label"],
        "purpose": pack.get("description", ""),
        "red_flag_checklist": pack.get("red_flags", []),
        "subject_inputs": inputs,
        "identity_gate": fetched["identity"],
        "deal_breaker_found_by_code": fetched["deal_breaker"],
        "evidence": [
            {"check": ev["label"], "tier": ev["tier"], "status": ev["status"], "facts": ev["facts"],
             "sources": [{k: s.get(k) for k in ("id", "title", "snippet", "reliability", "category")
                          if s.get(k)} for s in ev["sources"]]}
            for ev in fetched["results"]
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
# Step 4: Report  (scoring in plain code: predictable and explainable)
# ---------------------------------------------------------------------------

SEVERITY_POINTS = {"high": 3, "medium": 2, "low": 1}
TIER_WEIGHT = {1: 1.5, 2: 1.0, 3: 0.6}
RELIABILITY_WEIGHT = {"official": 1.0, "record": 0.9, "news": 0.7, "web": 0.4}
RELIABILITY_LABEL = {"official": "Official record", "record": "Data-provider record",
                     "news": "News report", "web": "Web page (weak)"}
HIGH_AT, MEDIUM_AT = 3.0, 1.4      # score thresholds for the overall risk level

RECOMMENDATIONS = {
    "Critical": "Do not proceed until compliance or legal has reviewed the deal-breaker.",
    "High": "Order a full paid background check before proceeding.",
    "Medium": "Clarify the flagged points with the subject before proceeding.",
    "Low": "Looks fine. Proceed with your normal process.",
}


def score_flag(flag: dict, rule_tiers: dict) -> dict:
    """score = severity points x tier weight (from the pack) x best source reliability."""
    best = max((f["reliability"] for f in flag["sources"]),
               key=lambda r: RELIABILITY_WEIGHT.get(r, 0.4), default="web")
    tier = rule_tiers.get(flag["rule_id"], 1 if flag["rule_id"] == "sanctions" else 2)
    score = SEVERITY_POINTS[flag["severity"]] * TIER_WEIGHT.get(tier, 1.0) * RELIABILITY_WEIGHT[best]
    return {**flag, "tier": tier, "reliability": best,
            "confidence": RELIABILITY_LABEL[best], "score": round(score, 2)}


def overall_risk(flags: list, identity: dict, deal_breaker: dict) -> str:
    if deal_breaker:
        return "Critical"
    top = max((f["score"] for f in flags), default=0)
    if top >= HIGH_AT:
        return "High"
    if top >= MEDIUM_AT or (identity and identity["confidence"] in ("low", "not_found")):
        return "Medium"     # an unconfirmed identity always deserves a second look
    return "Low"


def _is_clean_sanctions_flag(flag: dict) -> bool:
    """A sanctions flag must cite an actual list entry, not just 'the list we screened'.
    (Stops the LLM from turning a clean screen into a flag.)"""
    return flag["rule_id"] == "sanctions" and not any(
        (s["title"] or "").startswith("OFAC sanctions list:") for s in flag["sources"])


def _attach_sources(items: list, by_id: dict) -> list:
    """Replace source ids with real links. Drop items that cite no real source."""
    kept = []
    for item in items:
        srcs = [by_id[i] for i in item.get("source_ids", []) if i in by_id]
        if srcs:
            kept.append({**item, "sources": [
                {"id": s["id"], "title": s["title"], "url": s["url"],
                 "reliability": s.get("reliability", "web")} for s in srcs]})
    return kept


def run(pack_name: str, inputs: dict) -> dict:
    """The whole loop. Returns one dict the UI (or CLI) can render."""
    started = time.time()
    pack = load_pack(pack_name)
    fetched = run_checks(pack, inputs)
    results = fetched["results"]
    not_checked = fetched["not_checked"] + [
        {"item": item, "reason": "Not available in the data sources we use"}
        for item in pack.get("not_checked", [])]

    report = {"pack": pack["name"], "pack_label": pack["label"], "inputs": inputs,
              "checks": results, "not_checked": not_checked,
              "identity_gate": fetched["identity"], "deal_breaker": fetched["deal_breaker"],
              "skipped": fetched["skipped"]}

    analyzed = analyze(_llm_payload(pack, inputs, fetched)) if results else \
        {"ok": False, "error": "No checks could run. Provide more inputs."}
    if not analyzed["ok"]:
        report.update(status="error", error=analyzed["error"])
    else:
        a = analyzed["analysis"]
        by_id = {s["id"]: s for ev in results for s in ev["sources"]}
        rule_tiers = {r["id"]: r.get("tier", 2) for r in pack.get("red_flags", [])}
        flags = [score_flag(f, rule_tiers) for f in _attach_sources(a["red_flags"], by_id)
                 if not _is_clean_sanctions_flag(f)]
        flags.sort(key=lambda f: -f["score"])
        risk = overall_risk(flags, fetched["identity"], fetched["deal_breaker"])
        report.update(
            status="done", identity=a["identity"], red_flags=flags,
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
