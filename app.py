"""
app.py - Streamlit UI for the instant pre-screen agent.

Run:  streamlit run app.py
The form is built from the selected YAML pack, so new packs need no UI changes.
"""

import streamlit as st

import agent

st.set_page_config(page_title="Instant Pre-Screen", page_icon="🔎", layout="centered")

RISK_STYLE = {"Low": ("🟢", st.success), "Medium": ("🟠", st.warning), "High": ("🔴", st.error)}
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
STATUS_ICON = {"ok": "✅", "empty": "⚪", "error": "⚠️"}

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

with st.spinner("Running checks and looking for red flags..."):
    report = agent.run(pack_name, {k: v for k, v in inputs.items() if v})

# --- Report ----------------------------------------------------------------
if report["status"] == "error":
    st.error(report["error"])
else:
    icon, box = RISK_STYLE[report["risk_level"]]
    box(f"{icon} **Risk: {report['risk_level']}**. {report['recommendation']}")
    st.markdown(f"**Identified as:** {report['identity']}  \n"
                f"Identity confidence: `{report['identity_confidence']}`")
    st.write(report["summary"])

    st.subheader(f"Red flags ({len(report['red_flags'])})")
    if not report["red_flags"]:
        st.write("No red flags found in the data checked.")
    for f in report["red_flags"]:
        with st.container(border=True):
            st.markdown(f"{SEVERITY_ICON[f['severity']]} **{f['title']}**  "
                        f"`{f['severity']}` · `{f['rule_id']}`")
            st.write(f["detail"])
            st.caption(" · ".join(f"[{s['id']}: {(s['title'] or 'source')[:70]}]({s['url']})"
                                  for s in f["sources"]))
    if report.get("dropped_flags"):
        st.caption(f"{report['dropped_flags']} possible flag(s) were dropped because they "
                   "did not cite a real source.")

    if report["positives"]:
        st.subheader("Reassuring signals")
        for p in report["positives"]:
            st.markdown(f"✓ {p['detail']} "
                        + " ".join(f"[{s['id']}]({s['url']})" for s in p["sources"]))

st.subheader("Not checked")
st.caption("We never guess. These were not covered by this pre-screen:")
for n in report["not_checked"]:
    st.markdown(f"- **{n['item']}**: {n['reason']}")

st.subheader("Evidence")
for ev in report["checks"]:
    with st.expander(f"{STATUS_ICON[ev['status']]} {ev['label']}  ({len(ev['sources'])} sources)"):
        if ev["status"] == "empty":
            st.write("Nothing found.")
        if ev.get("error"):
            st.error(ev["error"])
        st.json(ev["facts"], expanded=False)
        for s in ev["sources"]:
            st.markdown(f"**{s['id']}** [{s['title'] or s['url']}]({s['url']})  \n"
                        f"<small>{s.get('snippet') or ''}</small>", unsafe_allow_html=True)

stats = report["stats"]
st.divider()
st.caption(f"⏱ {stats['seconds']} s · {stats['checks_run']} checks · "
           f"{stats['credits_used']} Crustdata credits (0 = served from cache). "
           "A traditional pre-screen takes 2–7 days and costs ₹500–₹8,000+.")
