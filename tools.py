"""
tools.py - thin wrappers around the Crustdata API. No business logic here.

Every successful response is cached to disk in cache/ as JSON, so the same
request never costs credits twice and demos keep working offline.
All functions return a dict and never raise:
  {"ok": True,  "data": <api json>, "from_cache": bool, "credits_used": str|None}
  {"ok": False, "error": "<readable message>"}
"""

import hashlib
import json
import time
from pathlib import Path

import requests

import config

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
TIMEOUT_SECONDS = 90


# ---------------------------------------------------------------------------
# Cache helpers (also used by agent.py to cache LLM answers)
# ---------------------------------------------------------------------------

def cache_key(prefix: str, payload) -> str:
    """Same prefix + same payload -> same file name. sort_keys keeps it stable."""
    raw = prefix + json.dumps(payload, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    slug = prefix.strip("/").replace("/", "_")
    return f"{slug}_{digest}"


def read_cache(key: str):
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def write_cache(key: str, data) -> None:
    path = CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "authorization": f"Bearer {config.CRUSTDATA_API_KEY}",
        "x-api-version": API_VERSION,
        "content-type": "application/json",
    }


def crustdata_post(endpoint: str, body: dict) -> dict:
    """POST to Crustdata, with disk caching. Errors are returned, not raised."""
    key = cache_key(endpoint, body)
    cached = read_cache(key)
    if cached is not None:
        return {**cached, "from_cache": True}

    if not config.CRUSTDATA_API_KEY:
        return {"ok": False, "error": "CRUSTDATA_API_KEY is empty. Add it to config.py."}
    for attempt in range(4):
        try:
            resp = requests.post(BASE_URL + endpoint, headers=_headers(), json=body,
                                 timeout=TIMEOUT_SECONDS)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Network error calling {endpoint}: {e}"}
        if resp.status_code != 429:
            break
        # 429 = rate limit (e.g. web search allows 10/minute): wait, then try again.
        time.sleep(15 * (attempt + 1))

    if resp.status_code != 200:
        # Errors are NOT cached: a retry later might succeed.
        return {"ok": False,
                "error": f"Crustdata {endpoint} returned {resp.status_code}: {resp.text[:300]}"}

    result = {"ok": True, "data": resp.json(),
              "credits_used": resp.headers.get("X-Credits-Used")}
    write_cache(key, result)
    return {**result, "from_cache": False}


# ---------------------------------------------------------------------------
# Endpoint wrappers (one function per endpoint we use)
# ---------------------------------------------------------------------------

# Only request the field groups we use: fewer premium groups = fewer credits.
PERSON_FIELDS = [
    "basic_profile",
    "social_handles.professional_network_identifier.profile_url",
    "experience.employment_details.current",
    "experience.employment_details.past",
    "education",          # +0.1 credit/result: lets us check education claims
]

COMPANY_FIELDS = ["basic_info", "headcount", "funding", "employee_reviews", "news"]


def person_search(name: str, company: str = "", limit: int = 3) -> dict:
    """/person/search by name, optionally also by a current-or-past company.
    "(.)" = case-insensitive, all words must appear (forgiving on spacing/case)."""
    name_cond = {"field": "basic_profile.name", "type": "(.)", "value": name}
    if company:
        filters = {"op": "and", "conditions": [
            name_cond,
            {"op": "or", "conditions": [
                {"field": "experience.employment_details.current.company_name",
                 "type": "(.)", "value": company},
                {"field": "experience.employment_details.past.company_name",
                 "type": "(.)", "value": company},
            ]},
        ]}
    else:
        filters = name_cond
    return crustdata_post("/person/search",
                          {"filters": filters, "fields": PERSON_FIELDS, "limit": limit})


def person_identify(linkedin_url: str) -> dict:
    """/person/identify by LinkedIn URL (exact lookup, 1 credit if matched)."""
    return crustdata_post("/person/identify",
                          {"professional_network_profile_urls": [linkedin_url]})


def company_enrich(name: str = "", domain: str = "") -> dict:
    """/company/enrich by domain (preferred, exact) or by name. ~2 credits."""
    body = {"domains": [domain]} if domain else {"names": [name]}
    body["fields"] = COMPANY_FIELDS
    return crustdata_post("/company/enrich", body)


def company_people(name: str = "", domain: str = "") -> dict:
    """/company/enrich with only the 'people' group (founders, CXOs). ~6 credits."""
    body = {"domains": [domain]} if domain else {"names": [name]}
    body["fields"] = ["people"]
    return crustdata_post("/company/enrich", body)


def web_search(query: str, sources=("news",), site: str = None) -> dict:
    """/web/search/live. 1 credit per call. Keep queries simple: quoted name + one keyword.
    site="indiankanoon.org" restricts results to that website."""
    body = {"query": query, "sources": list(sources)}
    if site:
        body["site"] = site
    return crustdata_post("/web/search/live", body)


# ---------------------------------------------------------------------------
# Free official public APIs (no key, no credits). Cached the same way.
# ---------------------------------------------------------------------------

def cached_get(url: str, params: dict = None, timeout: int = 45) -> dict:
    """GET a public JSON API with disk caching. Same return shape as crustdata_post."""
    key = cache_key("get_" + url.split("//")[-1].split("/")[0], {"url": url, "params": params})
    cached = read_cache(key)
    if cached is not None:
        return {**cached, "from_cache": True}
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error calling {url}: {e}"}
    if resp.status_code == 404:
        return {"ok": True, "data": None, "from_cache": False}   # "not found" is an answer
    if resp.status_code != 200:
        return {"ok": False, "error": f"{url} returned {resp.status_code}: {resp.text[:200]}"}
    result = {"ok": True, "data": resp.json()}
    write_cache(key, result)
    return {**result, "from_cache": False}


def nhtsa_decode_vin(vin: str) -> dict:
    """US NHTSA vPIC: decode a VIN into make, model, year (free, official)."""
    return cached_get(f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}",
                      {"format": "json"})


def nhtsa_recalls(make: str, model: str, year: str) -> dict:
    """US NHTSA: safety recalls for a make/model/year (free, official)."""
    return cached_get("https://api.nhtsa.gov/recalls/recallsByVehicle",
                      {"make": make, "model": model, "modelYear": year})


def propublica_search(name: str) -> dict:
    """ProPublica Nonprofit Explorer: find US nonprofits by name (IRS data, free)."""
    return cached_get("https://projects.propublica.org/nonprofits/api/v2/search.json", {"q": name})


def propublica_org(ein) -> dict:
    """ProPublica Nonprofit Explorer: one nonprofit's IRS Form 990 filings."""
    return cached_get(f"https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json")


def list_endpoints() -> dict:
    """Free call: which endpoints this API key may use. Not cached."""
    try:
        resp = requests.get(BASE_URL + "/account/endpoints", headers=_headers(),
                            params={"status": "enabled"}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    if resp.status_code != 200:
        return {"ok": False, "error": f"{resp.status_code}: {resp.text[:300]}"}
    return {"ok": True, "data": resp.json()}


if __name__ == "__main__":
    # `python tools.py` -> print which endpoints your key can use.
    result = list_endpoints()
    if not result["ok"]:
        print("ERROR:", result["error"])
    else:
        for ep in result["data"].get("endpoints", []):
            print(f'{ep["path"]:35} {ep.get("status"):9} '
                  f'credits/result={ep.get("base_credits")}  rpm={ep.get("effective_rate_limit_rpm")}')
