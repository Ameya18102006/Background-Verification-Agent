# Instant Pre-Screen Agent

Before you hire someone, sign a deal, or pay a vendor, the agent works out **who you are
dealing with** and points out **red flags** from public data in about a minute. Every flag
links to its source. It replaces the first step of a paid background check, which costs
₹500–₹8,000+ and takes 2–7 days.

Anything we can't check (police records, tax liens, financial statements, ...) is listed
under **Not checked**. The agent never guesses.

## What it checks, by tier

| Tier | Check | Data source | Source reliability |
|---|---|---|---|
| 1 | Identity gate: is this really the person/company? | Crustdata person / company | record |
| 1 | Sanctions screening | US Treasury OFAC lists (local, free) | official |
| 1 | Fraud and adverse media | Crustdata web search (news) | news |
| 1 | Court records | Crustdata web search on indiankanoon.org | official |
| 1 | Regulator orders | Crustdata web search on sebi.gov.in, sec.gov | official |
| 1–2 | Company status and age, headcount drop from peak | Crustdata company enrich | record |
| 2 | Founders' past ventures ("phoenix" pattern) | Crustdata company people + person search | record |
| 2 | Claims vs. records (jobs, titles, years, education) | Crustdata person profile | record |

## How it works

```
Pack (YAML) ─> Plan ─────────> Fetch ─────────────> Analyze ───────> Report
               order by tier   Tier 1 first;        one LLM call     score each flag =
               fill params     identity gate;       reads ONLY the   severity x tier x
                               deal-breaker skips   evidence, cites  source reliability;
                               Tier 2+ checks       source ids       risk in plain code
```

- **The domain lives in YAML.** A pack sets the inputs, the checks (and their tier), the
  red flags (and their tier, i.e. how much they weigh *in this domain*) and what's not
  covered. `agent.py` and `app.py` never mention hiring or vendors.
- **Domains reweight, not just select.** For example `failed_ventures` is Tier 1 for a
  business partner, while `job_hopping` is Tier 3 for hiring.
- **Severity × confidence.** Flag score = severity (high 3, medium 2, low 1) × tier weight
  (1.5 / 1.0 / 0.6) × source reliability (official 1.0, record 0.9, news 0.7, web 0.4).
  So a blog post never outranks a court record. Risk: Critical (deal-breaker found by code),
  High (score ≥ 3), Medium (score ≥ 1.4, or identity not confirmed), Low.
- **Short-circuit.** A strong sanctions match marks the report Critical and skips the
  lower-tier checks.
- **Identity gate.** The `gate: true` check gives a code-computed confidence (name similarity,
  company in job history, number of namesakes, exact LinkedIn URL or domain).
- **No made-up sources.** Each red flag must cite source ids, and flags without a real source
  are dropped. A site-restricted search keeps only results actually hosted on that site.

| File | What it does |
|---|---|
| `app.py` | Streamlit UI. The form is built from the selected pack |
| `agent.py` | Generic loop: `plan()`, `run_checks()`, `analyze()`, `score_flag()`, `run()` |
| `checks.py` | Check registry (`CHECKS`): person, company, sanctions, web search, key people |
| `tools.py` | Thin Crustdata wrappers with disk caching |
| `update_sanctions.py` | Downloads the OFAC lists into `data/sanctions/` |
| `config.py` | API keys (git-ignored) |
| `packs/*.yaml` | `hiring`, `business_partner`, `vendor` |

## Adding a new domain

Copy a pack, then change `inputs`, `checks`, `red_flags` and `not_checked`. The UI dropdown
picks it up automatically. You only write Python when the domain needs a new *kind* of data:
add one function to `checks.py` and register it in `CHECKS`.

Placeholders: `{name}` comes from the form, `{a|b}` means the first one that isn't empty,
and `{person.name}`, `{person.current_company}`, `{company.name}`, `{company.domain}` are
filled by earlier checks. Web-search queries can be `{q: ..., site: ..., category: ...}`.

## Setup (Windows PowerShell)

```powershell
pip install -r requirements.txt
```

```powershell
python update_sanctions.py
```

Put your keys in `config.py`. A key left as `""` falls back to the environment variable
with the same name. The model is `MODEL` in `agent.py`.

## Run

```powershell
streamlit run app.py
```

Pick a pack, click **Load demo case**, tick the consent box, then click **Run pre-screen**.
CLI version: `python agent.py vendor` (or `hiring`, `business_partner`).

Demo cases (public figures and companies, already cached):
- **Business partner:** Byju Raveendran / BYJU'S → High (fraud, insolvency, jail sentence, headcount down 46%)
- **Vendor:** Builder.ai → High (bankruptcy, fraud allegations, Indian Kanoon court records)
- **Hiring:** Satya Nadella with a false claim ("CEO since 2010") → claim mismatch flagged

## Credits (approximate)

| Call | Credits |
|---|---|
| `/person/search` (up to 3 results, with education) | ~0.3–1 |
| `/company/enrich` (status, headcount, funding, reviews, news) | 2–6 |
| `/company/enrich` people (founders, CXOs) | ~6 |
| `/web/search/live` (per query) | 1 |
| Sanctions screening | 0 (local files) |

One full run costs about 5–15 credits. Repeating a query costs 0, because every API
response and LLM answer is cached in `cache/`.
