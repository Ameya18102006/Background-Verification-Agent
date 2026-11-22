"""
tools.py - every call to an outside data source lives here.

Each public function = one check's data fetch (one tool call per check),
so when something breaks you know exactly where to look.

Every API response is cached to disk in cache/ as JSON. The same request
never costs credits twice, and the demo keeps working offline.
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
TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    """Build Crustdata auth headers. The key comes from config.py."""
    key = config.CRUSTDATA_API_KEY
    if not key:
        raise RuntimeError("CRUSTDATA_API_KEY is empty. Add it to config.py (see README.md).")
    return {
        "authorization": f"Bearer {key}",
        "x-api-version": API_VERSION,
        "content-type": "application/json",
    }


def cache_key(endpoint: str, body: dict) -> str:
    """Same endpoint + same body -> same file name. sort_keys keeps it stable."""
    raw = endpoint + json.dumps(body, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    slug = endpoint.strip("/").replace("/", "_")
    return f"{slug}_{digest}"


def _read_cache(key: str):
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _write_cache(key: str, data: dict) -> None:
    path = CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def crustdata_post(endpoint: str, body: dict) -> dict:
    """
    POST to Crustdata with disk caching.

    Always returns a dict shaped like:
      {"ok": True,  "data": <api json>, "from_cache": bool, "credits_used": str|None}
      {"ok": False, "error": "<readable message>"}
    so callers never have to catch exceptions.
    """
    key = cache_key(endpoint, body)
    cached = _read_cache(key)
    if cached is not None:
        cached["from_cache"] = True
        return cached

    try:
        resp = requests.post(BASE_URL + endpoint, headers=_headers(), json=body,
                             timeout=TIMEOUT_SECONDS)
    except RuntimeError as e:          # missing API key
        return {"ok": False, "error": str(e)}
    except requests.RequestException as e:  # network trouble
        return {"ok": False, "error": f"Network error calling {endpoint}: {e}"}

    if resp.status_code != 200:
        # Do NOT cache errors: a retry later might succeed.
        return {"ok": False,
                "error": f"Crustdata {endpoint} returned {resp.status_code}: {resp.text[:300]}"}

    result = {
        "ok": True,
        "request": {"endpoint": endpoint, "body": body},
        "data": resp.json(),
        "credits_used": resp.headers.get("X-Credits-Used"),
    }
    _write_cache(key, result)
    result["from_cache"] = False
    return result


# ---------------------------------------------------------------------------
# Tools (one per check)
# ---------------------------------------------------------------------------

# Only ask for the fields we use: fewer premium groups = fewer credits.
PERSON_FIELDS = [
    "basic_profile",
    "social_handles.professional_network_identifier.profile_url",
    "experience.employment_details.current",
    "experience.employment_details.past",
]


def person_search_body(name: str, company: str, limit: int = 3) -> dict:
    """
    Request body for /person/search: the person's name AND the claimed company
    appearing in either their current or past jobs.
    "(.)" = case-insensitive, all words must appear (forgiving on spacing/case).
    """
    return {
        "filters": {
            "op": "and",
            "conditions": [
                {"field": "basic_profile.name", "type": "(.)", "value": name},
                {
                    "op": "or",
                    "conditions": [
                        {"field": "experience.employment_details.current.company_name",
                         "type": "(.)", "value": company},
                        {"field": "experience.employment_details.past.company_name",
                         "type": "(.)", "value": company},
                    ],
                },
            ],
        },
        "fields": PERSON_FIELDS,
        "limit": limit,
    }


def crustdata_person(name: str, company: str) -> dict:
    """
    Check: person_profile. Find the person's professional profile.
    Returns crustdata_post()'s dict; data["profiles"] may be an empty list.
    """
    return crustdata_post("/person/search", person_search_body(name, company))


def list_endpoints() -> dict:
    """Free call: which endpoints this API key may use. Not cached (it's free)."""
    try:
        resp = requests.get(BASE_URL + "/account/endpoints", headers=_headers(),
                            params={"status": "enabled"}, timeout=TIMEOUT_SECONDS)
    except (RuntimeError, requests.RequestException) as e:
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
