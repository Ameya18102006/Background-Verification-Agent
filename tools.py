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
    try:
        resp = requests.post(BASE_URL + endpoint, headers=_headers(), json=body,
                             timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error calling {endpoint}: {e}"}

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


def web_search(query: str, sources=("news",)) -> dict:
    """/web/search/live. 1 credit per call. Keep queries simple: quoted name + one keyword."""
    return crustdata_post("/web/search/live", {"query": query, "sources": list(sources)})


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
