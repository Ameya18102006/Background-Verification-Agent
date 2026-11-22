"""
checks.py - the registry of reusable checks.

A check = one kind of data fetch (one tool call) + turning the raw JSON into
small, LLM-friendly FACTS plus a list of SOURCES (title + url) that red flags
can cite. Checks know nothing about hiring vs. business deals: the YAML pack
decides which checks run, with which inputs, and what counts as a red flag.

Adding a new kind of data = write one function here and add it to CHECKS.
Adding a new domain (used cars, landlords, ...) = write a new YAML pack only.

Every check returns an Evidence dict:
  status   : "ok" | "empty" (nothing found) | "error"
  facts    : dict of compact facts for the LLM
  sources  : [{"title", "url", "snippet"}]   (agent.py gives them ids S1, S2...)
  context  : values later checks can reuse, e.g. {"person.current_company": "Acme"}
  credits  : credits spent (None when cached)
  error    : message when status == "error"
"""

from datetime import date
from typing import Optional

import tools


def _evidence(status="ok", facts=None, sources=None, context=None, result=None, error=None):
    return {
        "status": status,
        "facts": facts or {},
        "sources": sources or [],
        "context": context or {},
        "credits": None if not result or result.get("from_cache") else result.get("credits_used"),
        "from_cache": bool(result and result.get("from_cache")),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Small date helpers (we do date maths in code, never in the LLM)
# ---------------------------------------------------------------------------

def _parse_date(value) -> Optional[date]:
    """Accepts '2021-03-01', '2021-03-01T00:00:00', '2021-03' or '2021'."""
    if not value:
        return None
    parts = str(value)[:10].split("-")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    nums += [1] * (3 - len(nums))
    try:
        return date(nums[0], nums[1], nums[2])
    except ValueError:
        return None


def _years(start: Optional[date], end: Optional[date]) -> Optional[float]:
    if not start:
        return None
    return round(((end or date.today()) - start).days / 365.25, 1)


# ---------------------------------------------------------------------------
# Check: person_profile
# ---------------------------------------------------------------------------

def _summarize_person(profile: dict, hint_company: str = "") -> dict:
    """Turn a Crustdata person profile into a compact job history + signals."""
    basic = profile.get("basic_profile") or {}
    employment = (profile.get("experience") or {}).get("employment_details") or {}
    url = (((profile.get("social_handles") or {})
            .get("professional_network_identifier") or {}).get("profile_url"))

    jobs = []
    for bucket in ("current", "past"):
        for job in employment.get(bucket) or []:
            start, end = _parse_date(job.get("start_date")), _parse_date(job.get("end_date"))
            is_current = bucket == "current" and not end
            jobs.append({
                "company": job.get("name") or job.get("company_name"),
                "title": job.get("title"),
                "start": str(start) if start else None,
                "end": "present" if is_current else (str(end) if end else None),
                "years": _years(start, None if is_current else end),
                "company_status": job.get("company_status"),   # e.g. "active", "closed"
                "company_headcount": job.get("company_headcount_latest"),
                "is_primary": bool(job.get("is_default")),   # the job shown as their main one
            })
    jobs.sort(key=lambda j: j["start"] or "", reverse=True)

    # Simple signals the LLM can reason about (computed here, not guessed).
    full_time = [j for j in jobs if j["years"] is not None]
    short_stints = [j for j in full_time if j["years"] < 1 and j["end"] != "present"]
    gaps = []
    ordered = sorted([j for j in jobs if j["start"]], key=lambda j: j["start"])
    latest_end = None
    for j in ordered:
        s = _parse_date(j["start"])
        if latest_end and s and (s - latest_end).days > 180:
            gaps.append({"from": str(latest_end), "to": str(s),
                         "months": round((s - latest_end).days / 30.4)})
        e = date.today() if j["end"] == "present" else _parse_date(j["end"])
        if e and (latest_end is None or e > latest_end):
            latest_end = e

    # People can hold several current roles (board seats, advisor...). Prefer the
    # one matching the company the user typed, then the primary job, then any.
    current = [j for j in jobs if j["end"] == "present"]
    typed = (hint_company or "").lower()
    main = (next((j for j in current if typed and typed in (j["company"] or "").lower()), None)
            or next((j for j in current if j["is_primary"]), None)
            or (current[0] if current else None))

    return {
        "name": basic.get("name"),
        "headline": basic.get("headline"),
        "location": (basic.get("location") or {}).get("raw"),
        "profile_url": url,
        "current_company": main["company"] if main else None,
        "current_title": main["title"] if main else None,
        "jobs": jobs[:15],
        "short_stints_under_1y": len(short_stints),
        "gaps_over_6_months": gaps,
    }


def person_profile(name: str = "", company: str = "", linkedin_url: str = "") -> dict:
    """Find a person (LinkedIn URL = exact; otherwise name + company search)."""
    if linkedin_url:
        result = tools.person_identify(linkedin_url)
        if not result["ok"]:
            return _evidence("error", result=result, error=result["error"])
        rows = result["data"] or []
        profiles = [m["person_data"] for r in rows for m in (r.get("matches") or [])]
    elif name:
        result = tools.person_search(name, company)
        if not result["ok"]:
            return _evidence("error", result=result, error=result["error"])
        profiles = result["data"].get("profiles") or []
    else:
        return _evidence("error", error="Need a name or a LinkedIn URL.")

    if not profiles:
        return _evidence("empty", facts={"searched_for": {"name": name, "company": company,
                                                          "linkedin_url": linkedin_url},
                                         "result": "not found"}, result=result)

    top = _summarize_person(profiles[0], company)
    facts = {**top, "matching_profiles": len(profiles)}
    if len(profiles) > 1:   # several people share this name: identity is less certain
        facts["other_matches"] = [
            {"name": (p.get("basic_profile") or {}).get("name"),
             "headline": (p.get("basic_profile") or {}).get("headline")}
            for p in profiles[1:]]
    sources = [{"title": f"Professional profile: {top['name']}", "url": top["profile_url"],
                "snippet": top.get("headline") or ""}]
    context = {"person.name": top["name"], "person.current_company": top["current_company"]}
    return _evidence("ok", facts, sources, context, result)


# ---------------------------------------------------------------------------
# Check: company_profile
# ---------------------------------------------------------------------------

def _headcount_signals(hc: dict) -> dict:
    """Peak vs. now from the weekly timeseries: a big drop is a classic red flag."""
    series = [p for p in (hc.get("timeseries") or []) if p.get("employee_count") is not None]
    out = {"total": hc.get("total"), "growth_percent": hc.get("growth_percent")}
    if series:
        peak = max(series, key=lambda p: p["employee_count"])
        latest = series[-1]
        out["peak"] = {"date": peak["date"], "employees": peak["employee_count"]}
        out["latest"] = {"date": latest["date"], "employees": latest["employee_count"]}
        if peak["employee_count"]:
            out["drop_from_peak_percent"] = round(
                100 * (peak["employee_count"] - latest["employee_count"]) / peak["employee_count"], 1)
    return out


def company_profile(name: str = "", domain: str = "") -> dict:
    """Company identity + health: headcount trend, funding, reviews, news."""
    if not (name or domain):
        return _evidence("error", error="Need a company name or domain.")
    result = tools.company_enrich(name=name, domain=domain)
    if not result["ok"]:
        return _evidence("error", result=result, error=result["error"])

    rows = result["data"] or []
    matches = rows[0].get("matches") if rows else []
    if not matches:
        return _evidence("empty", facts={"searched_for": domain or name, "result": "not found"},
                         result=result)

    best = matches[0]
    c = best.get("company_data") or {}
    info, funding = c.get("basic_info") or {}, c.get("funding") or {}
    reviews = c.get("employee_reviews") or {}
    overall = (reviews.get("overall_rating") or {})

    facts = {
        "name": info.get("name"),
        "match_confidence": best.get("confidence_score"),
        "other_candidates": len(matches) - 1,
        "domain": info.get("primary_domain"),
        "status": info.get("status"),
        "company_type": info.get("company_type"),
        "year_founded": info.get("year_founded"),
        "employee_count_range": info.get("employee_count_range"),
        "description": (info.get("description") or "")[:400],
        "headcount": _headcount_signals(c.get("headcount") or {}),
        "funding": {
            "total_usd": funding.get("total_investment_usd"),
            "last_round_type": funding.get("last_round_type"),
            "last_round_usd": funding.get("last_round_amount_usd"),
            "last_round_date": funding.get("last_fundraise_date"),
            "num_rounds": funding.get("num_funding_rounds"),
            "acquired_by": funding.get("acquired_by"),
        },
        "employee_reviews": {"rating": overall.get("rating"), "count": overall.get("total_count")},
    }
    record_url = info.get("professional_network_url") or info.get("website")
    sources = [{"title": f"Company record: {info.get('name')}", "url": record_url,
                "snippet": f"{info.get('company_type')}, founded {info.get('year_founded')}"}]
    # News attached to the company record. May be noisy: the LLM judges relevance.
    for article in (c.get("news") or [])[:8]:
        sources.append({"title": article.get("article_title"), "url": article.get("article_url"),
                        "snippet": f"{article.get('article_publisher_name')}, "
                                   f"{article.get('article_publish_date')}"})
    context = {"company.name": info.get("name"), "company.domain": info.get("primary_domain")}
    return _evidence("ok", facts, sources, context, result)


# ---------------------------------------------------------------------------
# Check: web_search  (adverse media, reviews, complaints... queries come from the pack)
# ---------------------------------------------------------------------------

def web_search(queries: list, sources_type: list = None, results_per_query: int = 5) -> dict:
    """Run each query from the pack (1 credit each) and collect results as sources."""
    sources, per_query, credits, errors = [], {}, 0.0, []
    all_cached = True
    for q in queries:
        result = tools.web_search(q, sources_type or ["news"])
        if not result["ok"]:
            errors.append(result["error"])
            continue
        all_cached &= bool(result.get("from_cache"))
        if not result.get("from_cache"):
            credits += float(result.get("credits_used") or 0)
        hits = (result["data"].get("results") or [])[:results_per_query]
        per_query[q] = len(hits)
        for h in hits:
            sources.append({"title": h.get("title"), "url": h.get("url"),
                            "snippet": h.get("snippet") or "", "query": q})

    if errors and not per_query:
        return _evidence("error", error="; ".join(errors))
    ev = _evidence("ok" if sources else "empty",
                   facts={"results_per_query": per_query,
                          "note": "Results may be about someone else with a similar name."},
                   sources=sources)
    ev["credits"] = None if all_cached else credits
    ev["from_cache"] = all_cached
    return ev


# ---------------------------------------------------------------------------
# The registry: tool name used in YAML packs -> (function, required params, label)
# ---------------------------------------------------------------------------

CHECKS = {
    "person_profile": {"fn": person_profile, "needs_any": ["name", "linkedin_url"],
                       "label": "Professional profile"},
    "company_profile": {"fn": company_profile, "needs_any": ["name", "domain"],
                        "label": "Company profile"},
    "web_search": {"fn": web_search, "needs_any": ["queries"],
                   "label": "Web and news search"},
}
