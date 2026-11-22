"""
app.py - Streamlit UI for the instant pre-screen agent.

Run:  streamlit run app.py
The form is built from the selected YAML pack, so new packs need no UI changes.
"""

import streamlit as st

import agent

st.set_page_config(page_title="Instant Pre-Screen", page_icon="🔎", layout="centered")

RISK_STYLE = {"Low": ("🟢", st.success), "Medium": ("🟠", st.warning),
              "High": ("🔴", st.error), "Critical": ("⛔", st.error)}
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
STATUS_ICON = {"ok": "✅", "empty": "⚪", "error": "⚠️"}
IDENTITY_ICON = {"high": "🟢", "medium": "🟡", "low": "🟠", "not_found": "🔴"}
TIER_NAME = {1: "Tier 1: deal-breakers", 2: "Tier 2: high-value signals", 3: "Tier 3: minor"}

st.title("🔎 Instant Pre-Screen")
st.caption("Finds who you're dealing with and points out red flags from public data, with "
           "a source link for every flag. It replaces the first step of a paid background check, "
           "which takes days and costs ₹500–₹8,000+.")

# --- Pick a pack -----------------------------------------------------------
packs = agent.list_packs()
pack_name = st.selectbox("What are you deciding?", list(packs),
                         format_func=lambda n: packs[n]["label"])
pack = packs[pack_name]
st.caption(pack.get("description", ""))

# Remember typed values per pack; "Load demo" fills them from the pack's demo case.
state_key = f"inputs_{pack_name}"
if state_key not in st.session_state:
    st.session_state[state_key] = {}
if pack.get("demo") and st.button("Load demo case"):
    st.session_state[state_key] = dict(pack["demo"])

# --- Input form, built from the pack --------------------------------------
with st.form(f"form_{pack_name}"):
    saved = st.session_state[state_key]
    inputs = {}
    for field in pack["inputs"]:
        label = field["label"] + (" *" if field.get("required") else "")
        widget = st.text_area if field.get("type") == "text" else st.text_input
        inputs[field["key"]] = widget(label, saved.get(field["key"], ""),
                                      key=f"{pack_name}_{field['key']}_{hash(str(saved))}").strip()
    consent = st.checkbox("I have a legitimate reason for this check (e.g. the person applied "
                          "or agreed to it), and I'll treat the results as leads to verify, not proof.")
    submitted = st.form_submit_button("Run pre-screen", type="primary")

if not submitted:
    st.stop()
st.session_state[state_key] = inputs

missing = [f["label"] for f in pack["inputs"] if f.get("required") and not inputs.get(f["key"])]
if missing:
    st.error("Please fill in: " + ", ".join(missing))
    st.stop()
if not consent:
    st.error("Please confirm you have a legitimate reason for this check.")
    st.stop()

with st.spinner("Running Tier 1 checks first, then the rest..."):
    report = agent.run(pack_name, {k: v for k, v in inputs.items() if v})

# --- Report: verdict -------------------------------------------------------
if report.get("deal_breaker"):
    st.error(f"⛔ **Deal-breaker found** ({report['deal_breaker']['check']}): "
             f"{report['deal_breaker']['reason']}. Lower-tier checks were skipped.")

if report["status"] == "error":
    st.error(report["error"])
else:
    icon, box = RISK_STYLE[report["risk_level"]]
    box(f"{icon} **Risk: {report['risk_level']}**. {report['recommendation']}")

# Identity gate: if we can't confirm who this is, every other finding is less reliable.
gate = report.get("identity_gate")
if gate:
    st.markdown(f"**Identity check:** {IDENTITY_ICON[gate['confidence']]} "
                f"`{gate['confidence']}` confidence"
                + (f" · {report['identity']}" if report.get("identity") else ""))
    st.caption(" · ".join(gate["reasons"]))
    if gate["confidence"] in ("low", "not_found"):
        st.warning("Identity not confirmed. Findings below may be about someone else. "
                   "Add a LinkedIn URL or website domain for an exact match.")

if report["status"] == "done":
    st.write(report["summary"])

    # --- Red flags (sorted by score = severity x tier x source reliability) ---
    st.subheader(f"Red flags ({len(report['red_flags'])})")
    if not report["red_flags"]:
        st.write("No red flags found in the data checked.")
    for f in report["red_flags"]:
        with st.container(border=True):
            st.markdown(f"{SEVERITY_ICON[f['severity']]} **{f['title']}**")
            st.caption(f"Severity `{f['severity']}` · Tier {f['tier']} · "
                       f"Source: **{f['confidence']}** · Score {f['score']}")
            st.write(f["detail"])
            st.caption(" · ".join(f"[{s['id']}: {(s['title'] or 'source')[:70]}]({s['url']})"
                                  for s in f["sources"]))
    if report.get("dropped_flags"):
        st.caption(f"{report['dropped_flags']} possible flag(s) were dropped because they "
                   "did not cite a real source.")
    st.caption("Score = severity × how much this flag matters in this domain (tier) × source "
               "reliability (official record > data-provider record > news > web page).")

    if report["positives"]:
        st.subheader("Reassuring signals")
        for p in report["positives"]:
            st.markdown(f"✓ {p['detail']} "
                        + " ".join(f"[{s['id']}]({s['url']})" for s in p["sources"]))

# --- Not checked -----------------------------------------------------------
st.subheader("Not checked")
st.caption("We never guess. These were not covered by this pre-screen:")
for n in report["not_checked"]:
    st.markdown(f"- **{n['item']}**: {n['reason']}")
if report.get("skipped"):
    st.caption("Not run because an optional input was left blank: " + ", ".join(report["skipped"]))

# --- Evidence, grouped by tier --------------------------------------------
st.subheader("Evidence")
for tier in sorted({ev["tier"] for ev in report["checks"]}):
    st.markdown(f"**{TIER_NAME.get(tier, f'Tier {tier}')}**")
    for ev in [e for e in report["checks"] if e["tier"] == tier]:
        with st.expander(f"{STATUS_ICON[ev['status']]} {ev['label']}  ({len(ev['sources'])} sources)"):
            if ev["status"] == "empty":
                st.write("Nothing found.")
            if ev.get("error"):
                st.error(ev["error"])
            st.json(ev["facts"], expanded=False)
            for s in ev["sources"]:
                st.markdown(f"**{s['id']}** · _{s.get('reliability', '')}_ · "
                            f"[{s['title'] or s['url']}]({s['url']})  \n"
                            f"<small>{s.get('snippet') or ''}</small>", unsafe_allow_html=True)

stats = report["stats"]
st.divider()
st.caption(f"⏱ {stats['seconds']} s · {stats['checks_run']} checks · "
           f"{stats['credits_used']} Crustdata credits (0 = served from cache). "
           "A traditional pre-screen takes 2–7 days and costs ₹500–₹8,000+.")
