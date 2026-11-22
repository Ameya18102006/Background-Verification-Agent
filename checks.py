"""
checks.py - the registry of reusable checks.

A check = one kind of data fetch + turning raw data into small, LLM-friendly
FACTS plus SOURCES (title + url + how reliable the source is). Checks know
nothing about hiring vs. vendors: the YAML pack decides which checks run, with
which inputs, at which tier, and what counts as a red flag.

Adding a new kind of data = write one function here and add it to CHECKS.
Adding a new domain = write a new YAML pack only.

Every check returns an Evidence dict:
  status       : "ok" | "empty" (nothing found) | "error"
  facts        : dict of compact facts for the LLM
  sources      : [{"title", "url", "snippet", "reliability"}]   (agent.py adds ids S1, S2...)
  context      : values later checks can reuse, e.g. {"person.current_company": "Acme"}
  identity     : (gate checks only) {"confidence": high|medium|low|not_found, "reasons": [...]}
  deal_breaker : None, or a short reason when the check found a stop-everything hit
  credits      : credits spent (None when cached)
  error        : message when status == "error"

Source reliability (used to weight red flags, so a weak source never outranks
an official record):
  official : government list, court or regulator website
  record   : structured data-provider record (Crustdata profile / company record)
  news     : news article
  web      : any other web page (blogs, forums, reviews)
"""

import csv
import re
from datetime import date
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import tools

# Websites whose pages are official records (courts, regulators, governments).
OFFICIAL_SITES = {
    "indiankanoon.org", "sebi.gov.in", "rbi.org.in", "mca.gov.in", "sec.gov",
    "justice.gov", "fca.org.uk", "find-and-update.company-information.service.gov.uk",
}

LEADERSHIP_WORDS = ("founder", "co-founder", "ceo", "chief executive", "director",
                    "owner", "managing partner", "proprietor", "president", "chairman")


def _evidence(status="ok", facts=None, sources=None, context=None, result=None, error=None,
              identity=None, deal_breaker=None):
    return {
        "status": status,
        "facts": facts or {},
        "sources": sources or [],
        "context": context or {},
        "identity": identity,
        "deal_breaker": deal_breaker,
        "credits": None if not result or result.get("from_cache") else result.get("credits_used"),
        "from_cache": bool(result and result.get("from_cache")),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Small helpers (date maths and name matching happen in code, never in the LLM)
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


def normalize_name(name: str) -> str:
    """'NADELLA, Satya.' -> 'nadella satya'. Drops punctuation and common company suffixes."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(pvt|private|ltd|limited|llc|inc|corp|corporation|co|plc|sa|gmbh)\b", " ", s)
    return " ".join(s.split())


def name_similarity(a: str, b: str) -> float:
    """0..1, ignores word order ('Satya Nadella' == 'NADELLA, Satya')."""
    ta, tb = sorted(normalize_name(a).split()), sorted(normalize_name(b).split())
    if not ta or not tb:
        return 0.0
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def _host(url: str) -> str:
    """'https://indiankanoon.org/doc/1/' -> 'indiankanoon.org'."""
    return (urlparse(url or "").hostname or "").lower()


def _is_leadership(title: str) -> bool:
    t = (title or "").lower()
    return any(w in t for w in LEADERSHIP_WORDS)


# ---------------------------------------------------------------------------
# Check: person_profile  (also the identity gate for person packs)
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
                "company_page_status": job.get("company_status"),  # "deleted" = page removed
                "company_headcount_now": job.get("company_headcount_latest"),
                "is_primary": bool(job.get("is_default")),
            })
    jobs.sort(key=lambda j: j["start"] or "", reverse=True)

    # Job-history signals, computed here (not guessed by the LLM).
    short_stints = [j for j in jobs if j["years"] is not None and j["years"] < 1
                    and j["end"] != "present"]
    gaps, latest_end = [], None
    for j in sorted([j for j in jobs if j["start"]], key=lambda j: j["start"]):
        s = _parse_date(j["start"])
        if latest_end and s and (s - latest_end).days > 180:
            gaps.append({"from": str(latest_end), "to": str(s),
                         "months": round((s - latest_end).days / 30.4)})
        e = date.today() if j["end"] == "present" else _parse_date(j["end"])
        if e and (latest_end is None or e > latest_end):
            latest_end = e

    # Ventures they led: founder/CEO/director roles. A deleted company page or a
    # tiny/zero headcount today can mean the venture failed (weak signal: verify).
    leadership = [{"company": j["company"], "title": j["title"], "start": j["start"],
                   "end": j["end"], "company_page_status": j["company_page_status"],
                   "company_headcount_now": j["company_headcount_now"]}
                  for j in jobs if _is_leadership(j["title"])]

    education = [{"school": e.get("school") or e.get("institute_name"),
                  "degree": e.get("degree") or e.get("degree_name"),
                  "field": e.get("field_of_study"),
                  "start": e.get("start_year") or e.get("start_date"),
                  "end": e.get("end_year") or e.get("end_date")}
                 for e in ((profile.get("education") or {}).get("schools") or [])]

    # People can hold several current roles (board seats...). Prefer the one
    # matching the company the user typed, then the primary job, then any.
    current = [j for j in jobs if j["end"] == "present"]
    typed = normalize_name(hint_company)
    main = (next((j for j in current if typed and typed in normalize_name(j["company"])), None)
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
        "education": education[:5],
        "short_stints_under_1y": len(short_stints),
        "gaps_over_6_months": gaps,
        "leadership_roles": leadership,
    }


def _person_identity(typed_name, typed_company, linkedin_url, top, n_profiles) -> dict:
    """How sure are we that this profile is the person the user means?"""
    reasons = []
    if linkedin_url:
        return {"confidence": "high", "reasons": ["Matched the exact LinkedIn URL provided"]}
    sim = name_similarity(typed_name, top["name"] or "")
    reasons.append(f"Name similarity {sim:.0%} ('{typed_name}' vs '{top['name']}')")
    company_hit = bool(typed_company) and any(
        normalize_name(typed_company) in normalize_name(j["company"] or "") for j in top["jobs"])
    if typed_company:
        reasons.append(("Company found" if company_hit else "Company NOT found")
                       + f" in job history ('{typed_company}')")
    if n_profiles > 1:
        reasons.append(f"{n_profiles} profiles matched the search; using the top one")

    if sim >= 0.9 and company_hit and n_profiles == 1:
        level = "high"
    elif sim >= 0.9 and (company_hit or n_profiles == 1):
        level = "medium"
    else:
        level = "low"
    return {"confidence": level, "reasons": reasons}


def person_profile(name: str = "", company: str = "", linkedin_url: str = "") -> dict:
    """Find a person (LinkedIn URL = exact; otherwise name + company search)."""
    if linkedin_url:
        result = tools.person_identify(linkedin_url)
        if not result["ok"]:
            return _evidence("error", result=result, error=result["error"])
        profiles = [m["person_data"] for r in (result["data"] or []) for m in (r.get("matches") or [])]
    elif name:
        result = tools.person_search(name, company)
        if not result["ok"]:
            return _evidence("error", result=result, error=result["error"])
        profiles = result["data"].get("profiles") or []
    else:
        return _evidence("error", error="Need a name or a LinkedIn URL.")

    if not profiles:
        return _evidence("empty", result=result,
                         facts={"searched_for": {"name": name, "company": company,
                                                 "linkedin_url": linkedin_url},
                                "result": "not found"},
                         identity={"confidence": "not_found",
                                   "reasons": ["No professional profile matched"]})

    top = _summarize_person(profiles[0], company)
    facts = {**top, "matching_profiles": len(profiles)}
    if len(profiles) > 1:
        facts["other_matches"] = [
            {"name": (p.get("basic_profile") or {}).get("name"),
             "headline": (p.get("basic_profile") or {}).get("headline")} for p in profiles[1:]]
    sources = [{"title": f"Professional profile: {top['name']}", "url": top["profile_url"],
                "snippet": top.get("headline") or "", "reliability": "record"}]
    context = {"person.name": top["name"], "person.current_company": top["current_company"]}
    identity = _person_identity(name, company, linkedin_url, top, len(profiles))
    return _evidence("ok", facts, sources, context, result, identity=identity)


# ---------------------------------------------------------------------------
# Check: company_profile  (identity gate for company packs + status/age/health)
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
    """Company identity + status, age, headcount trend, funding, reviews, news."""
    if not (name or domain):
        return _evidence("error", error="Need a company name or domain.")
    result = tools.company_enrich(name=name, domain=domain)
    if not result["ok"]:
        return _evidence("error", result=result, error=result["error"])

    rows = result["data"] or []
    matches = rows[0].get("matches") if rows else []
    if not matches:
        return _evidence("empty", result=result,
                         facts={"searched_for": domain or name, "result": "not found"},
                         identity={"confidence": "not_found",
                                   "reasons": ["No company record matched"]})

    best = matches[0]
    c = best.get("company_data") or {}
    info, funding = c.get("basic_info") or {}, c.get("funding") or {}
    overall = ((c.get("employee_reviews") or {}).get("overall_rating") or {})
    founded = info.get("year_founded")
    age = date.today().year - founded if founded else None
    headcount = _headcount_signals(c.get("headcount") or {})

    facts = {
        "name": info.get("name"),
        "domain": info.get("primary_domain"),
        "description": (info.get("description") or "")[:400],
        "company_type": info.get("company_type"),
        "employee_count_range": info.get("employee_count_range"),
        # Status and age signals (computed in code):
        "status": info.get("status"),
        "is_active": info.get("status") in (None, "active"),
        "year_founded": founded,
        "age_years": age,
        "is_young_company_under_2y": age is not None and age < 2,
        "headcount": headcount,
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
                "snippet": f"{info.get('company_type')}, founded {founded}, status {info.get('status')}",
                "reliability": "record"}]
    for article in (c.get("news") or [])[:8]:   # may be noisy: the LLM judges relevance
        sources.append({"title": article.get("article_title"), "url": article.get("article_url"),
                        "snippet": f"{article.get('article_publisher_name')}, "
                                   f"{article.get('article_publish_date')}",
                        "reliability": "news"})

    # Identity: a domain match is exact; a name match can hit a namesake company.
    reasons = []
    if domain and normalize_name(domain) in normalize_name(info.get("primary_domain") or ""):
        level = "high"
        reasons.append(f"Website domain matched ({domain})")
    else:
        sim = name_similarity(name, info.get("name") or "")
        reasons.append(f"Name similarity {sim:.0%} ('{name}' vs '{info.get('name')}')")
        if len(matches) > 1:
            reasons.append(f"{len(matches)} companies matched this name")
        level = "medium" if sim >= 0.85 and len(matches) == 1 else "low"

    context = {"company.name": info.get("name"), "company.domain": info.get("primary_domain")}
    return _evidence("ok", facts, sources, context, result,
                     identity={"confidence": level, "reasons": reasons})


# ---------------------------------------------------------------------------
# Check: sanctions_screen  (official OFAC lists, stored locally, free)
# ---------------------------------------------------------------------------

SANCTIONS_DIR = Path(__file__).parent / "data" / "sanctions"
OFAC_DETAILS = "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={}"
AKA = re.compile(r"a\.k\.a\.,?\s*'([^']+)'", re.I)


@lru_cache(maxsize=1)
def _load_sanctions() -> tuple:
    """Load OFAC entries once: [(normalized_name, display_name, entry_id, type, program)]."""
    entries = []

    def add(name, ent, typ, prog):
        norm = normalize_name(name)
        if norm:
            entries.append((norm, name.strip(), ent, typ, prog))

    for fname in ("SDN.CSV", "CONS_PRIM.CSV"):   # columns: id, name, type, program, ..., remarks
        path = SANCTIONS_DIR / fname
        if not path.exists():
            continue
        for row in csv.reader(path.open(encoding="utf-8", errors="replace")):
            if len(row) < 4:
                continue
            ent, name, typ, prog = row[0], row[1], row[2].strip(" -0"), row[3]
            add(name, ent, typ, prog)
            for alias in AKA.findall(row[-1] or ""):
                add(alias, ent, typ, prog)

    alt = SANCTIONS_DIR / "ALT.CSV"                 # columns: id, alt_id, alt_type, alt_name, ...
    if alt.exists():
        by_id = {e[2]: e for e in entries}
        for row in csv.reader(alt.open(encoding="utf-8", errors="replace")):
            if len(row) >= 4 and row[0] in by_id:
                _, _, _, typ, prog = by_id[row[0]]
                add(row[3], row[0], typ, prog)
    return tuple(entries)


def _screen(name: str, entries) -> list:
    """Strong = same words in any order. Possible = very similar spelling."""
    target = normalize_name(name)
    if len(target) < 4:
        return []
    t_tokens = sorted(target.split())
    hits = []
    for norm, display, ent, typ, prog in entries:
        if sorted(norm.split()) == t_tokens:
            hits.append(("strong", 1.0, display, ent, typ, prog))
        elif abs(len(norm) - len(target)) <= 4:
            score = SequenceMatcher(None, " ".join(sorted(norm.split())), " ".join(t_tokens)).ratio()
            if score >= 0.92:
                hits.append(("possible", round(score, 2), display, ent, typ, prog))
    hits.sort(key=lambda h: -h[1])
    seen, unique = set(), []
    for h in hits:                       # one hit per OFAC entry
        if h[3] not in seen:
            seen.add(h[3])
            unique.append(h)
    return unique[:5]


def sanctions_screen(names: list) -> dict:
    """Screen each name against OFAC sanctions lists. No API call, no credits."""
    entries = _load_sanctions()
    if not entries:
        return _evidence("error", error="Sanctions lists not downloaded. Run: python update_sanctions.py")

    results, sources, strong = {}, [], []
    for name in dict.fromkeys(n for n in names if n):          # de-duplicate, keep order
        hits = _screen(name, entries)
        results[name] = [{"match": h[0], "score": h[1], "listed_name": h[2],
                          "type": h[4] or "entity", "program": h[5]} for h in hits] or "no match"
        for h in hits:
            sources.append({"title": f"OFAC sanctions list: {h[2]} ({h[5]})",
                            "url": OFAC_DETAILS.format(h[3]),
                            "snippet": f"{h[0]} match for '{name}', score {h[1]}",
                            "reliability": "official"})
            if h[0] == "strong":
                strong.append(f"'{name}' matches OFAC entry '{h[2]}' ({h[5]})")

    sources.insert(0, {"title": f"OFAC sanctions lists ({len(entries):,} names and aliases screened)",
                       "url": "https://sanctionssearch.ofac.treas.gov/",
                       "snippet": "US Treasury SDN and consolidated lists", "reliability": "official"})
    facts = {"screened_names": results,
             "note": "A name match is not proof: confirm with date of birth, nationality or address."}
    return _evidence("ok", facts, sources,
                     deal_breaker=("; ".join(strong) if strong else None))


# ---------------------------------------------------------------------------
# Check: web_search  (adverse media, court records, regulator orders...)
# Queries come from the pack. Each can target one website and has a category.
# ---------------------------------------------------------------------------

def web_search(queries: list, results_per_query: int = 5) -> dict:
    """
    queries: list of strings, or dicts {"q", "category", "site", "reliability", "sources"}.
    "site" restricts to one website (e.g. indiankanoon.org for court judgments);
    results from OFFICIAL_SITES are marked as official records unless the pack sets
    "reliability" itself (e.g. "record" for a salvage-auction listing of a VIN).
    "sources" picks news or web results (default: news, or web when a site is given).
    """
    sources, per_query, credits, errors, all_cached = [], {}, 0.0, [], True
    for item in queries:
        spec = item if isinstance(item, dict) else {"q": item}
        q, site = spec.get("q", ""), spec.get("site")
        if not q:
            continue
        category = spec.get("category", "adverse_media")
        kinds = spec.get("sources") or (["web"] if site else ["news"])
        result = tools.web_search(q, kinds if isinstance(kinds, list) else [kinds], site=site)
        label = f"{q} [site:{site}]" if site else q
        if not result["ok"]:
            errors.append(result["error"])
            continue
        all_cached &= bool(result.get("from_cache"))
        if not result.get("from_cache"):
            credits += float(result.get("credits_used") or 0)
        hits = result["data"].get("results") or []
        if site:
            # The search engine sometimes ignores "site" and returns general web results
            # (blogs, reviews). Keep only results really hosted on that site.
            hits = [h for h in hits if _host(h.get("url")).endswith(site)]
        hits = hits[:results_per_query]
        per_query[label] = len(hits)
        reliability = spec.get("reliability") or (
            "official" if site in OFFICIAL_SITES else ("web" if site else "news"))
        for h in hits:
            sources.append({"title": h.get("title"), "url": h.get("url"),
                            "snippet": h.get("snippet") or "", "category": category,
                            "reliability": reliability})

    if errors and not per_query:
        return _evidence("error", error="; ".join(errors))
    ev = _evidence("ok" if sources else "empty",
                   facts={"results_per_query": per_query,
                          "note": "Results may be about someone else with a similar name."},
                   sources=sources)
    ev["credits"] = None if all_cached else credits
    ev["from_cache"] = all_cached
    if errors:
        ev["facts"]["errors"] = errors
    return ev


# ---------------------------------------------------------------------------
# Check: key_people  (company founders/CEOs and the ventures they led before)
# Catches the "phoenix company" pattern: same people, new company, old failures.
# ---------------------------------------------------------------------------

def key_people(name: str = "", domain: str = "", max_people: int = 2) -> dict:
    """Founders/CXOs of a company, then each person's leadership history."""
    result = tools.company_people(name=name, domain=domain)
    if not result["ok"]:
        return _evidence("error", result=result, error=result["error"])
    rows = result["data"] or []
    matches = rows[0].get("matches") if rows else []
    people = ((matches[0].get("company_data") or {}).get("people") or {}) if matches else {}

    leaders = []
    for p in (people.get("founders") or []) + (people.get("cxos") or []):
        b = p.get("basic_profile") or {}
        if b.get("name") and b["name"] not in [x["name"] for x in leaders]:
            leaders.append({"name": b["name"], "title": b.get("current_title")})
    if not leaders:
        return _evidence("empty", result=result,
                         facts={"result": "No founders or CXOs listed for this company"})

    facts, sources, credits = {"people": []}, [], float(result.get("credits_used") or 0) \
        if not result.get("from_cache") else 0.0
    company_label = name or domain
    for leader in leaders[:max_people]:
        ev = person_profile(leader["name"], company_label)
        credits += float(ev["credits"] or 0)
        if ev["status"] != "ok":
            facts["people"].append({**leader, "history": "profile not found"})
            continue
        f = ev["facts"]
        facts["people"].append({**leader, "profile_name": f["name"],
                                "leadership_roles": f["leadership_roles"],
                                "jobs": f["jobs"][:8]})
        sources += ev["sources"]
    out = _evidence("ok", facts, sources, result=result)
    out["credits"] = credits or None
    return out


# ---------------------------------------------------------------------------
# Check: vehicle_lookup  (US NHTSA: decode VIN + open safety recalls; free, official)
# ---------------------------------------------------------------------------

def vehicle_lookup(vin: str) -> dict:
    """Decode the VIN (is it a real, valid VIN? which car?) and list safety recalls."""
    vin = re.sub(r"[^A-Za-z0-9]", "", vin or "").upper()
    if len(vin) != 17:
        return _evidence("empty", facts={"vin": vin, "result": "VIN must be 17 characters"},
                         identity={"confidence": "not_found", "reasons": ["Invalid VIN length"]})
    result = tools.nhtsa_decode_vin(vin)
    if not result["ok"]:
        return _evidence("error", result=result, error=result["error"])
    v = (result["data"] or {}).get("Results", [{}])[0]
    make, model, year = v.get("Make"), v.get("Model"), v.get("ModelYear")
    clean = str(v.get("ErrorCode", "")).startswith("0")
    decode_url = f"https://vpic.nhtsa.dot.gov/decoder/Decoder?VIN={vin}"
    sources = [{"title": f"NHTSA VIN decode: {year} {make} {model}", "url": decode_url,
                "snippet": v.get("ErrorText", ""), "reliability": "official"}]

    recalls = []
    if make and model and year:
        rec = tools.nhtsa_recalls(make, model, year)
        for r in ((rec.get("data") or {}).get("results") or [])[:8] if rec["ok"] else []:
            recalls.append({"campaign": r.get("NHTSACampaignNumber"), "component": r.get("Component"),
                            "date": r.get("ReportReceivedDate"), "park_it": r.get("parkIt")})
            sources.append({"title": f"NHTSA recall {r.get('NHTSACampaignNumber')}: {r.get('Component')}",
                            "url": f"https://www.nhtsa.gov/recalls?nhtsaId={r.get('NHTSACampaignNumber')}",
                            "snippet": (r.get("Summary") or "")[:200], "reliability": "official"})

    facts = {"vin": vin, "decoded_ok": clean, "decode_message": v.get("ErrorText"),
             "make": make, "model": model, "year": year, "body": v.get("BodyClass"),
             "plant_country": v.get("PlantCountry"), "recalls_for_this_model_year": recalls,
             "note": "Recalls apply to the model year; check whether this car's repair was done."}
    identity = {"confidence": "high" if clean and make else "low",
                "reasons": [v.get("ErrorText") or "VIN not decoded"]}
    context = {"vehicle.vin": vin, "vehicle.make": make, "vehicle.model": model, "vehicle.year": year}
    return _evidence("ok" if make else "empty", facts, sources, context, result, identity=identity)


# ---------------------------------------------------------------------------
# Check: nonprofit_filings  (US IRS Form 990 data via ProPublica; free, official)
# ---------------------------------------------------------------------------

def nonprofit_filings(name: str = "", ein: str = "") -> dict:
    """Tax-exempt status and the last few years of IRS filings for a US nonprofit."""
    ein = re.sub(r"\D", "", ein or "")
    by_ein, candidates = bool(ein), 1
    if not ein:
        found = tools.propublica_search(name)
        if not found["ok"]:
            return _evidence("error", result=found, error=found["error"])
        orgs = (found["data"] or {}).get("organizations") or []
        orgs.sort(key=lambda o: -name_similarity(name, o.get("name", "")))
        if not orgs:
            return _evidence("empty", facts={"searched_for": name, "result": "not found in IRS data"},
                             identity={"confidence": "not_found",
                                       "reasons": ["No US nonprofit with this name in IRS data"]})
        ein, candidates = str(orgs[0]["ein"]), len(orgs)

    result = tools.propublica_org(ein)
    if not result["ok"]:
        return _evidence("error", result=result, error=result["error"])
    if not result["data"]:
        return _evidence("empty", facts={"ein": ein, "result": "EIN not found"},
                         identity={"confidence": "not_found", "reasons": ["EIN not found"]})
    org = result["data"]["organization"]
    filings = []
    for f in (result["data"].get("filings_with_data") or [])[:4]:
        exp = f.get("totfuncexpns") or 0
        filings.append({
            "year": f.get("tax_prd_yr"), "revenue": f.get("totrevenue"), "expenses": exp,
            "deficit": (f.get("totrevenue") or 0) < exp,
            "fundraising_cost_pct": round(100 * (f.get("profndraising") or 0) / exp, 1) if exp else None,
            "officer_pay_pct": round(100 * (f.get("pct_compnsatncurrofcr") or 0), 1),
            "net_assets": f.get("totnetassetend"), "pdf": f.get("pdf_url")})
    last_year = filings[0]["year"] if filings else None

    facts = {"name": org.get("name"), "ein": ein, "city": org.get("city"), "state": org.get("state"),
             "tax_exempt_section": f"501(c)({org.get('subsection_code')})",
             "deductible_donations": org.get("deductibility_code") == 1,
             "exempt_since": org.get("ruling_date"),
             "latest_filing_year": last_year,
             "years_since_last_filing": date.today().year - last_year if last_year else None,
             "filings": filings,
             "note": "US organizations only. No filings can mean small size or non-compliance."}
    page = f"https://projects.propublica.org/nonprofits/organizations/{ein}"
    sources = [{"title": f"IRS nonprofit record: {org.get('name')} (EIN {ein})", "url": page,
                "snippet": f"{facts['tax_exempt_section']}, exempt since {org.get('ruling_date')}",
                "reliability": "official"}]
    sources += [{"title": f"IRS Form 990, {f['year']}", "url": f["pdf"] or page,
                 "snippet": f"Revenue {f['revenue']}, expenses {f['expenses']}",
                 "reliability": "official"} for f in filings[:2]]

    sim = name_similarity(name, org.get("name", "")) if name else 0.0
    if by_ein:                                   # an EIN is an exact identifier
        conf, reasons = "high", [f"Matched by EIN {ein}"]
    else:
        conf = "high" if sim >= 0.85 and candidates == 1 else ("medium" if sim >= 0.85 else "low")
        reasons = [f"Name similarity {sim:.0%} with '{org.get('name')}'",
                   f"{candidates} nonprofits matched the name"]
    return _evidence("ok", facts, sources, {"org.name": org.get("name"), "org.ein": ein}, result,
                     identity={"confidence": conf, "reasons": reasons})


# ---------------------------------------------------------------------------
# The registry: tool name used in YAML packs -> function and defaults.
#   needs_any : at least one of these params must be filled, else the check is skipped
#   tier      : default tier (1 = deal-breaker checks, run first). Packs can override.
# ---------------------------------------------------------------------------

CHECKS = {
    "person_profile":   {"fn": person_profile, "needs_any": ["name", "linkedin_url"], "tier": 1,
                         "label": "Professional profile and identity"},
    "company_profile":  {"fn": company_profile, "needs_any": ["name", "domain"], "tier": 2,
                         "label": "Company status, age and health"},
    "sanctions_screen": {"fn": sanctions_screen, "needs_any": ["names"], "tier": 1,
                         "label": "Sanctions screening (OFAC)"},
    "web_search":       {"fn": web_search, "needs_any": ["queries"], "tier": 1,
                         "label": "Web and news search"},
    "key_people":       {"fn": key_people, "needs_any": ["name", "domain"], "tier": 2,
                         "label": "Founders and their past ventures"},
    "vehicle_lookup":   {"fn": vehicle_lookup, "needs_any": ["vin"], "tier": 1,
                         "label": "Vehicle identity and safety recalls (NHTSA)"},
    "nonprofit_filings": {"fn": nonprofit_filings, "needs_any": ["name", "ein"], "tier": 1,
                          "label": "Tax-exempt status and IRS filings"},
}
