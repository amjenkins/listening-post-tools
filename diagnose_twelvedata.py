#!/usr/bin/env python3
"""
diagnose_twelvedata.py — one-off diagnostic, NOT part of the production
verification job.

Purpose: find out, empirically, which of the six Macro Indicators rows are
actually reachable on Max's Twelve Data free-tier API key. Twelve Data's own
docs are ambiguous/silent about whether indices (S&P 500, DXY) are free-tier
and explicit that commodities (Brent crude, gold) require a paid ($79/mo)
plan — rather than guess symbols and plan tiers the way the Stooq setup did
(which caused a real, avoidable live failure), this script asks the API
directly and reports back what it actually says.

Reads the API key from the TWELVE_DATA_API_KEY repository secret (an
environment variable here) — never printed, never embedded in a logged
URL. GitHub Actions also auto-masks any exact occurrence of a registered
secret in log output, as a second layer of protection.

This does NOT touch data/latest.json and is not on any schedule — it only
runs when triggered by hand from the Actions tab, and its only output is
this human-readable log.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")

# One test case per Macro Indicators row. `symbol` is what we send to
# Twelve Data's /quote endpoint; `note` is just for the printed report.
CANDIDATES = [
    {"key": "sp500", "label": "S&P 500", "symbol": "SPX"},
    {"key": "brent_crude", "label": "Brent Crude", "symbol": "BRENT"},
    {"key": "gold", "label": "Gold (spot)", "symbol": "XAU/USD"},
    {"key": "dxy", "label": "US Dollar Index (DXY)", "symbol": "DXY"},
    {"key": "eurusd", "label": "EUR/USD", "symbol": "EUR/USD"},
    {"key": "usdjpy", "label": "USD/JPY", "symbol": "USD/JPY"},
]


def _quote(symbol):
    url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={API_KEY}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def check(candidate):
    label = candidate["label"]
    symbol = candidate["symbol"]
    try:
        raw = _quote(symbol)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a diagnostic
        # Do not print str(exc) verbatim if it might embed the request URL;
        # rebuild a safe message instead.
        print(f"{label:24s} symbol={symbol:10s} NETWORK ERROR ({type(exc).__name__})")
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"{label:24s} symbol={symbol:10s} NON-JSON RESPONSE: {raw[:150]!r}")
        return

    if isinstance(data, dict) and (data.get("status") == "error" or "code" in data and "close" not in data):
        code = data.get("code", "?")
        message = data.get("message", "(no message)")
        print(f"{label:24s} symbol={symbol:10s} ERROR  code={code}  message={message}")
    elif isinstance(data, dict) and "close" in data:
        print(f"{label:24s} symbol={symbol:10s} OK     close={data.get('close')}  "
              f"datetime={data.get('datetime')}  exchange={data.get('exchange', data.get('mic_code', 'n/a'))}")
    else:
        print(f"{label:24s} symbol={symbol:10s} UNEXPECTED SHAPE: {json.dumps(data)[:150]}")


def main():
    if not API_KEY:
        print("TWELVE_DATA_API_KEY is not set — check the repository secret name matches exactly.")
        return 0

    print("Twelve Data plan-coverage check — one row per line, real answers only:\n")
    for candidate in CANDIDATES:
        check(candidate)
        time.sleep(1)  # stay well under free-tier rate limits (8 credits/min)

    print("\nDone. Paste this whole log block back to Claude — it contains no secret values.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
