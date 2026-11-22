"""
update_sanctions.py - download the official US Treasury (OFAC) sanctions lists.

Run once before the demo (and whenever you want fresh data):
    python update_sanctions.py

Files are public domain, free, and saved to data/sanctions/:
  SDN.CSV        main Specially Designated Nationals list
  ALT.CSV        aliases ("also known as") for SDN entries
  CONS_PRIM.CSV  OFAC's other (non-SDN) consolidated lists
"""

from pathlib import Path

import requests

BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
FILES = ["SDN.CSV", "ALT.CSV", "CONS_PRIM.CSV"]
OUT_DIR = Path(__file__).parent / "data" / "sanctions"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        resp = requests.get(BASE + name, timeout=120)
        resp.raise_for_status()
        (OUT_DIR / name).write_bytes(resp.content)
        print(f"Saved {name}: {len(resp.content) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
