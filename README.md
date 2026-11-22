# Instant Pre-Screen Agent

Before you hire someone, a public-data check runs in about a minute and gives you a
report with source links. It replaces the first step of a paid background check
(₹500–₹8,000+, 2–7 days).

**What it checks (Level 1):** whether a candidate's claimed company, role and years
match their professional profile on Crustdata.
**What it does NOT check:** criminal records, court cases, education certificates.
Crustdata doesn't hold that data, so we never guess at it.

## How it works

```
Plan  ->  Fetch (Crustdata /person/search)  ->  Facts (dates/tenure in Python)
      ->  Analyze (one Gemini call, strict JSON)  ->  Report (+ source URL per check)
```

| File | What it does |
|---|---|
| `app.py` | Streamlit UI |
| `agent.py` | The loop: `plan()`, `fetch()`, `extract_facts()`, `analyze()`, `run()` |
| `tools.py` | Crustdata calls with disk caching (`cache/`) |
| `packs/` | Check packs (YAML); used from Level 3 on |
| `make_demo_cache.py` | Writes the offline demo case (a made-up **SAMPLE** profile) |

## Setup (Windows PowerShell)

```powershell
pip install -r requirements.txt
```

Put your keys in `config.py` (it is in `.gitignore`, so never commit it):

```python
CRUSTDATA_API_KEY = "your-crustdata-key" or os.environ.get(...)
GEMINI_API_KEY = "your-gemini-key" or os.environ.get(...)
```

If a key is left as `""`, the environment variable with the same name is used instead.
Get a Gemini key at https://aistudio.google.com/apikey. The model is set by `MODEL` in `agent.py` (`gemini-3.5-flash`).

See which Crustdata endpoints your key can use (this call is free):

```powershell
python tools.py
```

## Run

```powershell
python make_demo_cache.py
streamlit run app.py
```

Click **Load demo case**, then **Run pre-screen**.

CLI version of the demo: `python agent.py`

## Credits and caching

- `/person/search` costs about 0.13 credits per result (basic profile + experience). We fetch up to 3 results per check.
- Every successful response is saved in `cache/`. Repeating a query costs nothing.
- LLM answers are cached too, so a demo you've already run once works with no network.
