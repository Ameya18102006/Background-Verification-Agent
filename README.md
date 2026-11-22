# Instant Pre-Screen Agent

Before you hire someone, sign a deal, or pay a vendor, the agent works out **who you are
dealing with** and points out **red flags** from public data in under a minute. Every flag
links to its source. It replaces the first step of a paid background check, which costs
₹500–₹8,000+ and takes 2–7 days.

It does not check criminal records, court filings or financials, because Crustdata doesn't
hold that data. Anything we can't check is listed under **Not checked**. The agent never guesses.

## How it works

```
Pack (YAML) ──> Plan ──> Fetch ──> Analyze ──> Report
                 │         │          │           │
   fill inputs into   one tool call   one LLM call    risk + recommendation
   each check's       per check ->    reads ONLY the  computed in code from
   params, skip       facts + sources evidence, cites the flags
   what's missing     (S1, S2, ...)   source ids
```

- **The domain lives in YAML.** A pack lists the inputs to ask for, the checks to run, the
  red flags that matter for that domain, and what's not covered. `agent.py` and `app.py`
  never mention hiring or vendors.
- **Checks are reusable building blocks** (`checks.py`): `person_profile`, `company_profile`
  and `web_search`. Each one makes one tool call and returns facts plus sources.
- **Checks can chain.** A check can use what an earlier one found, e.g.
  `{person.current_company}` (the company found on the person's profile).
- **No made-up sources.** Each red flag must cite source ids. Flags that cite no real
  source are dropped in code.
- **Date maths in code** (tenure, gaps, headcount drop from peak), so the LLM never does arithmetic.

| File | What it does |
|---|---|
| `app.py` | Streamlit UI. The form is built from the selected pack |
| `agent.py` | Generic loop: `run_checks()`, `analyze()`, `score()`, `run()` |
| `checks.py` | Check registry (`CHECKS`). Turns API JSON into facts + sources |
| `tools.py` | Thin Crustdata wrappers with disk caching |
| `config.py` | API keys (git-ignored) |
| `packs/*.yaml` | `hiring`, `business_partner`, `vendor` |

## Adding a new domain

Copy a pack, change `inputs`, `checks` (with `{placeholders}`), `red_flags` and
`not_checked`. That's all: the UI dropdown picks it up automatically.
You only write Python when the domain needs a new *kind* of data: add one function to
`checks.py` and register it in `CHECKS`.

Placeholders: `{name}` comes from the form, `{a|b}` means the first one that isn't empty,
and `{person.name}`, `{person.current_company}`, `{company.name}`, `{company.domain}` are
filled by earlier checks.

## Setup (Windows PowerShell)

```powershell
pip install -r requirements.txt
```

Put your keys in `config.py`. A key left as `""` falls back to the environment variable
with the same name. The model is `MODEL` in `agent.py`.

```powershell
python tools.py          # free: lists the endpoints your Crustdata key can use
```

## Run

```powershell
streamlit run app.py
```

Pick a pack, click **Load demo case**, tick the consent box, then click **Run pre-screen**.
CLI version: `python agent.py business_partner` (or `hiring`, `vendor`).

Demo cases (public figures and companies, already cached):
- **Business partner:** Byju Raveendran / BYJU'S → High risk (insolvency, court cases, headcount down 46%)
- **Vendor:** Builder.ai → High risk (bankruptcy, fraud allegations)
- **Hiring:** Satya Nadella with a false claim ("CEO since 2010") → claim mismatch flagged

## Credits (approximate)

| Call | Credits |
|---|---|
| `/person/search` (up to 3 results) | ~0.2–0.7 |
| `/company/enrich` (basic_info, headcount, funding, reviews, news) | 2–6 |
| `/web/search/live` (per query) | 1 |

One full run costs about 5–10 credits. Repeating a query costs 0, because every API
response and LLM answer is cached in `cache/`.
