"""
make_demo_cache.py - writes the fixed offline demo case into cache/.

The profile below is MADE UP (fictional person, fictional company) and is
marked "_sample": True so the UI shows a SAMPLE DATA banner.
It mimics the shape of a real Crustdata /person/search response.

Once you have a real demo person, just run the app on them once:
the real response is cached automatically and you can switch DEMO_CLAIM.
"""

import agent
import tools

SAMPLE_RESPONSE = {
    "profiles": [
        {
            "crustdata_person_id": 0,
            "basic_profile": {
                "name": "Aarav Mehta",
                "headline": "Data Scientist at Nimbus Analytics",
                "current_title": "Data Scientist",
                "location": {"raw": "Bengaluru, Karnataka, India"},
            },
            "social_handles": {
                "professional_network_identifier": {
                    "profile_url": "https://www.linkedin.com/in/sample-aarav-mehta"
                }
            },
            "experience": {
                "employment_details": {
                    "current": [
                        {"name": "Nimbus Analytics", "title": "Data Scientist",
                         "start_date": "2023-06-01", "end_date": None}
                    ],
                    "past": [
                        {"name": "Nimbus Analytics", "title": "Data Analyst",
                         "start_date": "2022-07-01", "end_date": "2023-05-31"},
                        {"name": "Orbit Retail", "title": "Business Analyst Intern",
                         "start_date": "2022-01-01", "end_date": "2022-06-30"},
                    ],
                }
            },
        }
    ],
    "next_cursor": None,
    "total_count": 1,
}

if __name__ == "__main__":
    d = agent.DEMO_CLAIM
    key = tools.cache_key("/person/search", tools.person_search_body(d["name"], d["company"]))
    tools._write_cache(key, {"ok": True, "_sample": True, "data": SAMPLE_RESPONSE,
                             "credits_used": None})
    print(f"Wrote cache/{key}.json")
