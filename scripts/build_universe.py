#!/usr/bin/env python3
"""
Build/refresh the `universe` list in data/state.json from NSE Indices'
own published Nifty 500 constituent file.

This is a SEPARATE, occasional step from the nightly scan
(rotation_sweep_v3_full500.py) — the Nifty 500 is only reconstituted by
NSE roughly twice a year, so there is no need to run this nightly. Run it
once to seed `universe` for the first time, and again whenever you want
to pick up an index reconstitution.

Source (NSE Indices' own site, not a third party):
    https://niftyindices.com/IndexConstituent/ind_nifty500list.csv

That CSV has historically had these columns: "Company Name", "Industry",
"Symbol", "Series", "ISIN Code". This script validates the header row
against what it expects and fails loudly — rather than silently
guessing — if NSE has changed the format; open the CSV in a browser/
spreadsheet app and adjust EXPECTED_COLUMNS below if that happens.

Usage:
    python scripts/build_universe.py                 # fetch + write
    python scripts/build_universe.py --dry-run        # fetch + preview only
    python scripts/build_universe.py --csv path.csv   # use an already-
                                                        # downloaded CSV
                                                        # instead of fetching

Only the `universe` key in state.json is touched. `coverage`,
`market_regime`, `breadth`, `sector_summary`, and everything else the
nightly scan writes is left exactly as-is.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import List, Optional

import requests

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "state.json"
SOURCE_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"

# NSE's site is picky about a browser-like User-Agent on this endpoint.
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; universe-builder/1.0)"}

EXPECTED_COLUMNS = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}


def fetch_csv_text(url: str = SOURCE_URL) -> str:
    r = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_universe(csv_text: str) -> List[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("CSV appears empty — got no header row at all.")

    header = {h.strip() for h in reader.fieldnames}
    missing = {"Symbol", "ISIN Code"} - header
    if missing:
        raise ValueError(
            f"Expected columns {missing} not found in the CSV header {sorted(header)}. "
            "NSE may have changed the file format — open the CSV manually and update "
            "this script's column names before proceeding."
        )
    has_industry = "Industry" in header
    has_series = "Series" in header

    universe = []
    seen_symbols = set()
    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        isin = (row.get("ISIN Code") or "").strip()
        if not symbol or not isin:
            continue  # skip blank/malformed rows rather than fabricating a record
        if has_series and (row.get("Series") or "").strip().upper() != "EQ":
            continue  # keep only the standard equity trading series
        if symbol in seen_symbols:
            continue  # defensive de-dupe; the source file shouldn't have duplicates
        seen_symbols.add(symbol)
        entry = {"symbol": symbol, "isin": isin}
        if has_industry:
            sector = (row.get("Industry") or "").strip()
            if sector:
                entry["sector"] = sector
        universe.append(entry)
    return universe


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"universe": [], "coverage": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=False))


def summarize_change(old_universe: List[dict], new_universe: List[dict]) -> str:
    old_symbols = {e["symbol"] for e in old_universe}
    new_symbols = {e["symbol"] for e in new_universe}
    added = sorted(new_symbols - old_symbols)
    removed = sorted(old_symbols - new_symbols)
    lines = [f"Old universe: {len(old_universe)} symbols", f"New universe: {len(new_universe)} symbols"]
    if added:
        lines.append(f"Added ({len(added)}): {', '.join(added[:20])}{' ...' if len(added) > 20 else ''}")
    if removed:
        lines.append(f"Removed ({len(removed)}): {', '.join(removed[:20])}{' ...' if len(removed) > 20 else ''}")
    if not added and not removed:
        lines.append("No membership change (symbol set is identical).")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse and print a summary, but don't write state.json")
    parser.add_argument("--csv", type=Path, default=None, help="Use a local CSV file instead of fetching from NSE")
    args = parser.parse_args(argv)

    if args.csv:
        csv_text = args.csv.read_text()
    else:
        print(f"Fetching {SOURCE_URL} ...")
        csv_text = fetch_csv_text()

    new_universe = parse_universe(csv_text)
    if not new_universe:
        print("ERROR: parsed zero symbols — refusing to write an empty universe.", file=sys.stderr)
        return 1

    state = load_state()
    old_universe = state.get("universe", [])
    print(summarize_change(old_universe, new_universe))

    if args.dry_run:
        print("\n--dry-run set: state.json NOT modified.")
        return 0

    state["universe"] = new_universe
    save_state(state)
    print(f"\nWrote {len(new_universe)} symbols to {STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
