#!/usr/bin/env python3
"""
Batch ingest all valid BTC updown 5m/15m events for 2026-06-19.

Usage:
  PYTHONPATH=/Users/yfclark/nautilus_trader \
  /Users/yfclark/nautilus_trader/.venv/bin/python3 ingest_all_btc_updown_2026-06-19.py

This will download (if not local) and write instrument + data for each.
Run in background or overnight as there are ~377 events.
"""
import subprocess
import sys
from pathlib import Path

PYTHON = "/Users/yfclark/nautilus_trader/.venv/bin/python3"
SCRIPT = "nautilus_pmdata_ingest.py"
ENV = {"PYTHONPATH": "/Users/yfclark/nautilus_trader"}

def run_ingest(slug: str):
    cmd = [PYTHON, SCRIPT, "--slug", slug]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, env=ENV | {"PATH": "/usr/bin:/bin"})
    if result.returncode != 0:
        print(f"  ERROR for {slug}: {result.stderr[-200:]}")
    else:
        print(f"  OK for {slug}")

def main():
    base = Path(__file__).parent
    for res in ["5m", "15m"]:
        listfile = base / f"btc_updown_{res}_2026-06-19.txt"
        if not listfile.exists():
            print(f"Missing {listfile}, run discovery first.")
            continue
        slugs = [line.strip() for line in listfile.read_text().splitlines() if line.strip()]
        print(f"Ingesting {len(slugs)} {res} events...")
        for slug in slugs:
            run_ingest(slug)

if __name__ == "__main__":
    main()
