"""
app.py - Streamlit UI for the instant pre-screen agent.

Run:  streamlit run app.py
"""

import streamlit as st

import agent

st.set_page_config(page_title="Instant Pre-Screen", page_icon="🔎", layout="centered")

STATUS_STYLE = {
    "match": ("✅", "Match"),
    "partial": ("🟡", "Partial"),
    "mismatch": ("❌", "Mismatch"),
    "not_found": ("⚪", "Not found"),
}

st.title("🔎 Instant Pre-Screen")
st.caption("Checks a candidate's career claim against public professional data "
           "(Crustdata) in under a minute. Level 1: hiring.")

# --- Input form ------------------------------------------------------------
if "form" not in st.session_state:
    st.session_state.form = {"name": "", "company": "", "role": "", "years": 1.0}

if st.button("Load demo case"):
    d = agent.DEMO_CLAIM
    st.session_state.form = {**d, "years": float(d["years"])}

with st.form("claim"):
    f = st.session_state.form
    name = st.text_input("Candidate name", f["name"])
    company = st.text_input("Claimed company", f["company"])
    role = st.text_input("Claimed role", f["role"])
    years = st.number_input("Claimed years at that company", 0.0, 50.0, f["years"], 0.5)
    submitted = st.form_submit_button("Run pre-screen", type="primary")

if not submitted:
    st.stop()

if not name.strip() or not company.strip():
    st.error("Name and company are required.")
    st.stop()

claim = {"name": name.strip(), "company": company.strip(), "role": role.strip(), "years": years}

with st.spinner("Fetching profile and comparing claims..."):
    report = agent.run(claim)

# --- Report ----------------------------------------------------------------
fetched = report.get("fetch", {})

if report["status"] == "error":
    st.error(report["error"])
    st.stop()

if fetched.get("is_sample"):
    st.warning("SAMPLE DATA: this profile is a made-up fixture for the offline demo, "
               "not a real person.")

facts = fetched.get("facts")
if facts:
    st.subheader("Profile found")
    st.markdown(f"**{facts['name']}** · {facts.get('headline') or ''}")
    if facts.get("location"):
        st.caption(facts["location"])
    if facts.get("profile_url"):
        st.markdown(f"Source: [{facts['profile_url']}]({facts['profile_url']})")
    if fetched.get("candidates", 0) > 1:
        st.info(f"{fetched['candidates']} profiles matched; showing the top one.")

    st.markdown("**Jobs at the claimed company** (dates from the profile, tenure computed in code)")
    if facts["jobs_at_claimed_company"]:
        st.table([{"Title": j["title"], "Company": j["company"], "From": j["start_date"],
                   "To": j["end_date"], "Years": j["years"]}
                  for j in facts["jobs_at_claimed_company"]])
    else:
        st.write("None found.")

if report["status"] == "partial":
    st.error(report["error"])
    st.stop()

st.subheader("Claim vs. reality")
for c in report["checks"]:
    icon, label = STATUS_STYLE.get(c["status"], ("•", c["status"]))
    with st.container(border=True):
        st.markdown(f"{icon} **{c['claim'].title()}**: {label}")
        st.markdown(f"Claimed: `{c['claimed']}`  \nFound: `{c['found']}`")
        st.caption(c["explanation"])
        if c.get("source_url"):
            st.caption(f"Source: {c['source_url']}")

st.subheader("Summary")
st.write(report["summary"])

with st.expander("Debug: raw report"):
    st.caption(f"Crustdata from cache: {fetched.get('from_cache')} · "
               f"credits used: {fetched.get('credits_used') or '0 (cached)'} · "
               f"LLM from cache: {report.get('llm_from_cache')}")
    st.json(report)
