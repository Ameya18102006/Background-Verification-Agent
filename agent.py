"""
agent.py - the pre-screen agent: Plan -> Fetch -> Analyze -> Report.

Level 1 (hiring only):
  Input : name, company, claimed role, claimed years at that company
  Fetch : one Crustdata /person/search call
  Facts : plain Python pulls the matching jobs + tenure out of the profile
          (we do the date maths in code, not in the LLM)
  LLM   : one Gemini call compares claim vs. facts, returns strict JSON
  Report: per-claim match / mismatch / not found, each with a source URL

Golden rule: the LLM only sees data we fetched. It never answers from memory.
"""

import hashlib
import json
import sys
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel

import config
import tools

MODEL = "gemini-3.5-flash"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

class ClaimCheck(BaseModel):
    """One line of the report: one claim and what the data says about it."""
    claim: Literal["identity", "company", "role", "years"]
    claimed: str
    found: str          # what the data shows, or "not found"
    status: Literal["match", "partial", "mismatch", "not_found"]
    explanation: str    # one or two plain sentences


class Analysis(BaseModel):
    """What the LLM must return (validated by the SDK)."""
    checks: list[ClaimCheck]
    summary: str


# ---------------------------------------------------------------------------
# Step 1: Plan
# ---------------------------------------------------------------------------

def plan(claim: dict) -> list[str]:
    """Level 1 always runs one check. Level 3 will read this from a YAML pack."""
    return ["person_profile"]


# ---------------------------------------------------------------------------
# Step 2: Fetch  (+ turn raw JSON into simple facts)
# ---------------------------------------------------------------------------

def _parse_date(value) -> Optional[date]:
    """Accepts '2021-03-01', '2021-03-01T00:00:00', '2021-03' or '2021'."""
    if not value:
        return None
    s = str(value)[:10]
    for fmt_len, parts in ((10, 3), (7, 2), (4, 1)):
        try:
            bits = [int(x) for x in s[:fmt_len].split("-")]
            if len(bits) == parts:
                return date(bits[0], bits[1] if parts > 1 else 1, bits[2] if parts > 2 else 1)
        except ValueError:
            continue
    return None


def _years_between(start: Optional[date], end: Optional[date]) -> Optional[float]:
    if not start:
        return None
    end = end or date.today()
    return round((end - start).days / 365.25, 1)


def _company_matches(job_company: str, claimed_company: str) -> bool:
    """Loose match: 'Infosys' vs 'Infosys Limited' should count."""
    a, b = (job_company or "").lower().strip(), claimed_company.lower().strip()
    return bool(a and b) and (a in b or b in a)


def extract_facts(profile: dict, claimed_company: str) -> dict:
    """Pull out only what the comparison needs. Pure Python, no LLM."""
    basic = profile.get("basic_profile") or {}
    employment = (profile.get("experience") or {}).get("employment_details") or {}
    url = (((profile.get("social_handles") or {})
            .get("professional_network_identifier") or {}).get("profile_url"))

    jobs = []
    for bucket in ("current", "past"):
        for job in employment.get(bucket) or []:
            company = job.get("name") or job.get("company_name") or ""
            start, end = _parse_date(job.get("start_date")), _parse_date(job.get("end_date"))
            jobs.append({
                "company": company,
                "title": job.get("title"),
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else ("present" if bucket == "current" else None),
                "years": _years_between(start, end if bucket == "past" else None),
                "is_current": bucket == "current",
                "matches_claimed_company": _company_matches(company, claimed_company),
            })

    at_company = [j for j in jobs if j["matches_claimed_company"]]
    total_years = round(sum(j["years"] or 0 for j in at_company), 1) if at_company else None

    return {
        "name": basic.get("name"),
        "headline": basic.get("headline"),
        "location": (basic.get("location") or {}).get("raw"),
        "profile_url": url,
        "jobs_at_claimed_company": at_company,
        "total_years_at_claimed_company": total_years,
        "all_jobs": jobs,
    }


def fetch(claim: dict) -> dict:
    """Run the person lookup and return facts (or a clean 'not found')."""
    result = tools.crustdata_person(claim["name"], claim["company"])
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}

    profiles = result["data"].get("profiles") or []
    base = {"ok": True, "from_cache": result.get("from_cache"),
            "credits_used": result.get("credits_used"),
            "is_sample": bool(result.get("_sample")),
            "candidates": len(profiles)}
    if not profiles:
        return {**base, "facts": None}
    # Level 1: take the top hit. (Level 5 idea: let the user pick if several.)
    return {**base, "facts": extract_facts(profiles[0], claim["company"])}


# ---------------------------------------------------------------------------
# Step 3: Analyze  (one LLM call, strict JSON)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a careful background-verification analyst doing a pre-screen.
You compare what a candidate CLAIMS against PROFILE DATA fetched from a professional-data provider.

Rules:
- Use ONLY the profile data given. Never use outside knowledge or guess.
- If the data does not contain something, the status is "not_found" and found is "not found".
- Tenure numbers are already computed for you; use them, do not recompute.
- role: "match" if the titles mean the same job (e.g. "SDE II" ~ "Software Engineer II"),
  "partial" if same field but a different level, "mismatch" if clearly different.
- years: "match" if within 0.5 years, "partial" if within 1.5 years, otherwise "mismatch".
- Be neutral and factual. A mismatch is a reason to ask questions, not proof of lying.
Return exactly one check for each of: identity, company, role, years."""


def _llm_cache_key(claim: dict, facts: dict) -> str:
    raw = json.dumps({"model": MODEL, "prompt": SYSTEM_PROMPT, "claim": claim, "facts": facts},
                     sort_keys=True)
    return "llm_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def analyze(claim: dict, facts: dict) -> dict:
    """Ask Gemini to compare claim vs. facts. Cached to disk like API calls."""
    key = _llm_cache_key(claim, facts)
    cached = tools._read_cache(key)
    if cached:
        return {"ok": True, "analysis": cached, "from_cache": True}

    try:
        from google import genai
        if not config.GEMINI_API_KEY:
            return {"ok": False, "error": "GEMINI_API_KEY is empty. Add it to config.py (see README.md)."}
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        user_msg = ("CLAIM:\n" + json.dumps(claim, indent=2) +
                    "\n\nPROFILE DATA:\n" + json.dumps(facts, indent=2))
        response = client.models.generate_content(
            model=MODEL,
            contents=user_msg,
            config={
                "system_instruction": SYSTEM_PROMPT,
                "response_mime_type": "application/json",  # strict JSON out
                "response_schema": Analysis,               # validated against our Pydantic model
                "temperature": 0,                          # same input -> same verdict
            },
        )
    except Exception as e:  # missing key, network, rate limit... show it, don't crash
        return {"ok": False, "error": f"LLM call failed: {type(e).__name__}: {e}"}

    if response.parsed is None:
        return {"ok": False, "error": f"LLM gave no usable answer: {(response.text or '')[:300]}"}

    analysis = response.parsed.model_dump()
    tools._write_cache(key, analysis)
    return {"ok": True, "analysis": analysis, "from_cache": False}


# ---------------------------------------------------------------------------
# Step 4: Report
# ---------------------------------------------------------------------------

def run(claim: dict) -> dict:
    """The whole loop. Returns one dict the UI (or CLI) can render."""
    report = {"claim": claim, "checks_planned": plan(claim)}

    fetched = fetch(claim)
    report["fetch"] = fetched
    if not fetched["ok"]:
        report["status"] = "error"
        report["error"] = fetched["error"]
        return report

    facts = fetched["facts"]
    if facts is None:
        # Nothing to compare. Say so plainly - never guess.
        report["status"] = "not_found"
        report["checks"] = [
            {"claim": c, "claimed": str(claim.get(k, "")), "found": "not found",
             "status": "not_found", "explanation": "No matching professional profile was found.",
             "source_url": None}
            for c, k in (("identity", "name"), ("company", "company"),
                         ("role", "role"), ("years", "years"))
        ]
        report["summary"] = ("No public professional profile matched this name and company. "
                             "That is not proof of anything, but the claim could not be verified.")
        return report

    analyzed = analyze(claim, facts)
    if not analyzed["ok"]:
        report["status"] = "partial"   # facts are still shown in the UI
        report["error"] = analyzed["error"]
        return report

    # Attach the source link in code, so the LLM can never invent a URL.
    checks = analyzed["analysis"]["checks"]
    for c in checks:
        c["source_url"] = facts["profile_url"]
    report["status"] = "done"
    report["checks"] = checks
    report["summary"] = analyzed["analysis"]["summary"]
    report["llm_from_cache"] = analyzed["from_cache"]
    return report


# A fixed demo case. Its Crustdata response is saved in cache/ (see README).
DEMO_CLAIM = {
    "name": "Aarav Mehta",
    "company": "Nimbus Analytics",
    "role": "Senior Data Scientist",
    "years": 4,
}


if __name__ == "__main__":
    # `python agent.py`  -> run the demo case and print the report as JSON
    print(json.dumps(run(DEMO_CLAIM), indent=2, ensure_ascii=False))
    sys.exit(0)
