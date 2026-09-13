#!/usr/bin/env python3
"""
diagnose_yahoo_gold.py — one-off diagnostic, NOT part of the production
verification job.

Purpose: find Yahoo Finance's real symbol for spot gold. The previous
guess, "XAUUSD=X", 404'd on the live run. Rather than guess again, this
tests several plausible candidates directly against Yahoo's chart API
(the same endpoint verify_indicators.py uses) from GitHub Actions, which
has real internet access (unlike the sandbox this was written in, and
unlike Claude's own WebFetch tool, which robots.txt blocks from this
endpoint entirely — see the main script's docstring).

GC=F (gold futures — the symbol already confirmed working) is included as
a control, so a clean run should show at least one success.

This does NOT touch data/latest.json and is not on any schedule — it only
runs when triggered by hand from the Actions tab.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CANDIDATES = ["GC=F", "XAU=X", "XAUUSD=X", "XAUUSD", "GOLD"]


def check(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print(f"{symbol:12s} HTTP ERROR {exc.code} ({exc.reason})")
        return
    except Exception as exc:  # noqa: BLE001 - diagnostic, report and move on
        print(f"{symbol:12s} NETWORK ERROR ({type(exc).__name__}: {exc})")
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"{symbol:12s} NON-JSON RESPONSE: {raw[:150]!r}")
        return

    result = data.get("chart", {}).get("result")
    if not result:
        err = data.get("chart", {}).get("error")
        print(f"{symbol:12s} NO RESULT (error={err})")
        return

    result = result[0]
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote", [{}])[0] or {}).get("close") or []
    for ts, close in zip(reversed(timestamps), reversed(closes)):
        if close is not None:
            as_of = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            long_name = result.get("meta", {}).get("longName") or result.get("meta", {}).get("shortName") or "?"
            print(f"{symbol:12s} OK   close={close}  as_of={as_of}  name={long_name!r}")
            return
    print(f"{symbol:12s} OK-SHAPE but no non-null close in range")


def main():
    print("Yahoo Finance spot-gold symbol check — real answers only:\n")
    for symbol in CANDIDATES:
        check(symbol)
        time.sleep(1)
    print("\nDone. Paste this whole log block back to Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
