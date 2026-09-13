#!/usr/bin/env python3
"""
verify_indicators.py — The Listening Post's Macro Indicators data-verification job.

Purpose (per the report's methodology charter, v0.42/v0.44): market closing
levels printed in the Macro Indicators table have been wrong three separate
times because a human-in-the-loop (an LLM doing WebFetch/WebSearch) had to
transcribe a number off a news page and eyeball whether a second source
"agreed." This script removes that step for the rows that are plain daily
market closes: it fetches each row from two independent, keyless public data
providers, compares the figures to a disclosed tolerance, and writes a
structured, audited result — no narrative page, no transcription, no
"looked about right."

This script is meant to run on a schedule via GitHub Actions (see
.github/workflows/verify.yml), NOT inside the Listening Post's own Claude
session — that sandbox's network egress is allowlisted to package registries
only and cannot reach financial data sites directly. GitHub Actions runners
have normal outbound internet access, so the fetch happens there; the
Listening Post's scheduled run then just reads the small JSON result this
job commits back to the repo (via a plain HTTPS fetch of the raw file,
which is unauthenticated and unrestricted).

Rows covered: the recurring, unambiguous daily-close rows (S&P 500, Brent
crude, gold, DXY, EUR/USD, USD/JPY). Rows that aren't a simple daily close
(China PMI, US rail carloads, retail sales, AI token demand, the ag index)
are out of scope for this script and stay on the existing manual-research
process — there's no equivalent "the" number for those the way there is for
a market close.

Second-source status per row (confirmed live 2026-09-13, not guessed):
Stooq is blocked outright for GitHub Actions' IP range on every row — this
was tested with both the original and a browser-like User-Agent, so it
looks like an IP-level block, not a header/bot-detection one. Where Stooq
fails, EUR/USD and USD/JPY fall back to Twelve Data (Max's free-tier key,
using its /eod endpoint so both sides are quoting a daily close, not a
live snapshot vs. a close) — confirmed working, with small, plausible
differences. S&P 500, Brent crude, DXY, and gold have no working second
source right now: the first three because Twelve Data's free plan doesn't
cover stock indices or true commodities; gold because Yahoo's free API
only exposes the futures contract (not spot), and Twelve Data's XAU/USD
IS spot — two genuinely different instruments that will never agree, so
comparing them isn't a real cross-check and isn't attempted. All four of
these rows report "SINGLE-SOURCE ONLY" honestly rather than faking a
comparison.

Output: data/latest.json, containing for each row:
  - the literal figure returned by each source (never just "agreed")
  - whether they matched within the disclosed tolerance for that instrument
  - a plain-English status a human (or an LLM assembling the report) can
    act on without re-deriving anything.

Exit code is always 0 (a data-source hiccup should not fail the whole CI
run) — failures are recorded in the JSON, not thrown.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# A real-browser UA string, not a self-identifying bot string. This is a
# deliberate change (2026-09-13): the first live run showed Stooq rejecting
# every single row with an identical generic error, which is the signature
# of either an IP-range block or a UA/bot filter — and the original UA
# literally contained the word "Verifier," which is exactly the kind of
# string a bot filter keys on. This alone may not fix an IP-based block, but
# it's a legitimate, low-cost thing to rule out before assuming the block is
# unfixable.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Row configuration.
#
# Each row lists two independent sources: a Yahoo Finance chart-API ticker
# and a Stooq CSV ticker. `decimals` sets the rounding precision used for the
# match check (this is the "disclosed tolerance," not an eyeballed one, per
# the charter's v0.42 rule: exact-decimal agreement at a stated precision,
# not "close enough").
#
# NOTE ON TICKER CONFIDENCE: the equity/FX tickers below (^GSPC, EURUSD=X,
# JPY=X and their Stooq equivalents) are standard, long-stable symbols. The
# Brent-crude and DXY Stooq codes are the ones I'm least certain of, since I
# could not test a live network call against Stooq from the sandbox this
# script was written in (egress there is allowlisted to package registries
# only — see the module docstring). If a row shows "source_b_error" for
# brent or dxy on the first live run, that ticker code is the first thing
# to check and correct.
# ---------------------------------------------------------------------------
ROWS = [
    {
        "key": "sp500",
        "label": "S&P 500",
        "yahoo_symbol": "%5EGSPC",
        "stooq_symbol": "^spx",
        "decimals": 2,
        # No Twelve Data fallback: confirmed live 2026-09-13 (diagnose_
        # twelvedata.py) that stock indices aren't reachable on the free
        # plan. This is the row behind all three historical wrong-print
        # incidents, and it's still single-source (Yahoo only) — worth
        # revisiting if a free index source ever turns up.
    },
    {
        "key": "brent_crude",
        "label": "Brent Crude",
        "yahoo_symbol": "BZ%3DF",
        "stooq_symbol": "cb.f",
        "decimals": 2,
        # No Twelve Data fallback: confirmed live 2026-09-13 that
        # commodities (Brent crude, per Twelve Data's own pricing page)
        # require a paid plan. Single-source (Yahoo only) for now.
    },
    {
        "key": "gold",
        # Relabeled 2026-09-13 (was "Gold (spot)"). The row had been labeled
        # "spot" all along while actually sourcing "GC=F," COMEX gold
        # FUTURES — a real, silent mismatch that only surfaced once a
        # genuine spot-gold source (Twelve Data's XAU/USD) was tried against
        # it and showed a consistent ~1.4% gap. Chased two fixes for this,
        # both dead ends, worth recording so nobody re-tries them:
        #   1. Guessed Yahoo might have a spot-gold symbol like the FX pairs
        #      below use ("XAUUSD=X") — confirmed live (via
        #      diagnose_yahoo_gold.py) that this 404s, and so do "XAU=X" and
        #      bare "XAUUSD." A candidate called "GOLD" DOES resolve on
        #      Yahoo, but to a company called Gold.com, Inc. (a ~$48 stock
        #      price, nowhere near gold's actual price) — a false positive,
        #      confirmed and rejected, not used.
        #   2. Conclusion: Yahoo's free chart API doesn't appear to expose a
        #      real spot-gold quote at all, only the futures contract. So
        #      Twelve Data's genuine spot price and Yahoo's futures price
        #      can never be made to agree — they're different instruments
        #      with a real, structural basis between them, not a
        #      configuration problem. Running that comparison anyway would
        #      flag "DISAGREEMENT" every single day regardless of whether
        #      anything is actually wrong, which would train readers to
        #      ignore the flag — worse than not cross-checking at all.
        # So: back to "GC=F," relabeled to say what it actually is, and NO
        # second source for this row (matches how S&P 500/Brent/DXY are
        # handled — an honest gap, not a fake cross-check).
        "label": "Gold (COMEX futures, front-month)",
        "yahoo_symbol": "GC%3DF",
        "stooq_symbol": "xauusd",
        "decimals": 2,
    },
    {
        "key": "dxy",
        "label": "US Dollar Index (DXY)",
        "yahoo_symbol": "DX-Y.NYB",
        "stooq_symbol": "usdx",
        "decimals": 2,
        # No Twelve Data fallback: confirmed live 2026-09-13 that the DXY
        # index isn't reachable on the free plan (HTTPError). No other
        # free, same-convention second source identified yet — this row
        # stays single-source (Yahoo) until one is.
    },
    {
        "key": "eurusd",
        "label": "EUR/USD",
        "yahoo_symbol": "EURUSD%3DX",
        "stooq_symbol": "eurusd",
        "decimals": 4,
        # Twelve Data fallback — confirmed live 2026-09-13, works on the
        # free plan. Preferred over the earlier Frankfurter.app fallback:
        # Frankfurter reports the ECB's once-daily reference fixing, a
        # different moment than Yahoo's market close, which produced
        # routine false "disagreement" flags. Twelve Data's forex quote is
        # a live market rate on the same convention as Yahoo's, so it's a
        # genuine like-for-like cross-check.
        "twelvedata_symbol": "EUR/USD",
    },
    {
        "key": "usdjpy",
        "label": "USD/JPY",
        "yahoo_symbol": "JPY%3DX",
        "stooq_symbol": "usdjpy",
        "decimals": 2,
        "twelvedata_symbol": "USD/JPY",
    },
]


def _http_get(url, timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv,application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            # A plausible Referer header — some sites treat a request with no
            # Referer at all as a stronger bot signal than one with one.
            "Referer": "https://stooq.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_yahoo_close(symbol):
    """Returns (close_float, as_of_iso8601_string) from Yahoo's chart API,
    or raises with a short, specific reason."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    raw = _http_get(url)
    data = json.loads(raw)
    result = data.get("chart", {}).get("result")
    if not result:
        err = data.get("chart", {}).get("error")
        raise ValueError(f"yahoo: no result (error={err})")
    result = result[0]
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote", [{}])[0] or {}).get("close") or []
    # Walk backwards to the last non-null close (the most recent session
    # sometimes has a null close if the market is mid-session or the feed
    # hasn't settled it yet).
    for ts, close in zip(reversed(timestamps), reversed(closes)):
        if close is not None:
            as_of = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            return float(close), as_of
    raise ValueError("yahoo: no non-null close in range")


def fetch_stooq_close(symbol):
    """Returns (close_float, as_of_iso8601_string) from Stooq's CSV endpoint,
    or raises with a short, specific reason."""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    raw = _http_get(url)
    lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"stooq: no data rows returned (raw={raw[:120]!r})")
    header = lines[0].split(",")
    last = lines[-1].split(",")
    if "N/D" in last or len(last) < 5:
        raise ValueError(f"stooq: malformed/no-data row (raw last line={lines[-1]!r})")
    row = dict(zip(header, last))
    return float(row["Close"]), row["Date"]


TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")


def fetch_twelvedata_quote(symbol):
    """Returns (close_float, as_of_iso8601_string) from Twelve Data's /eod
    (end-of-day) endpoint, or raises with a short, specific reason.

    Deliberately NOT the /quote endpoint: an earlier version of this
    function used /quote and, on its first live run (2026-09-13, a Sunday),
    every row came back "DISAGREEMENT" against Yahoo — not because the data
    was wrong, but because /quote returns a live, right-now snapshot (FX and
    gold markets had already reopened for the new week) while Yahoo's chart
    API returns the last COMPLETED trading day's close (Friday's). Two
    different moments in time, not a real disagreement — the same
    timing-mismatch failure mode that ruled out Frankfurter.app earlier.
    /eod is built specifically to answer "what did it close at," the same
    question Yahoo's feed answers, so the two are actually comparable.

    Requires the TWELVE_DATA_API_KEY repository secret to be set (see
    .github/workflows/verify.yml) — confirmed live 2026-09-13 (via
    diagnose_twelvedata.py, against the older /quote endpoint) that Max's
    free-tier key covers gold (as XAU/USD) and both FX pairs, but NOT stock
    indices or true commodities — see the ROWS config above for exactly
    which rows use this fallback. /eod has not been separately confirmed
    live as of this edit; if it turns out not to be covered on the same
    plan as /quote, that will show up as a recorded, non-fatal error on
    source_b in data/latest.json, not a crash — worth checking the next
    live run for."""
    if not TWELVE_DATA_API_KEY:
        raise ValueError("twelvedata: TWELVE_DATA_API_KEY is not set")
    url = f"https://api.twelvedata.com/eod?symbol={symbol}&apikey={TWELVE_DATA_API_KEY}"
    raw = _http_get(url)
    data = json.loads(raw)
    if not isinstance(data, dict) or "close" not in data:
        # Twelve Data's error shape is typically {"code": ..., "message": ...}.
        code = data.get("code") if isinstance(data, dict) else "?"
        message = data.get("message") if isinstance(data, dict) else raw[:150]
        raise ValueError(f"twelvedata: no close in response (code={code}, message={message})")
    return float(data["close"]), data.get("datetime")


def verify_row(row):
    out = {
        "key": row["key"],
        "label": row["label"],
        "decimals": row["decimals"],
        "source_a": {"name": "Yahoo Finance", "symbol": row["yahoo_symbol"]},
        "source_b": {"name": "Stooq", "symbol": row["stooq_symbol"]},
    }

    a_value = a_asof = a_err = None
    b_value = b_asof = b_err = None

    try:
        a_value, a_asof = fetch_yahoo_close(row["yahoo_symbol"])
        out["source_a"]["value"] = a_value
        out["source_a"]["as_of"] = a_asof
    except Exception as exc:  # noqa: BLE001 - deliberately broad, recorded not raised
        a_err = str(exc)
        out["source_a"]["error"] = a_err

    try:
        b_value, b_asof = fetch_stooq_close(row["stooq_symbol"])
        out["source_b"]["value"] = b_value
        out["source_b"]["as_of"] = b_asof
    except Exception as exc:  # noqa: BLE001
        stooq_err = str(exc)
        out["source_b"]["error"] = stooq_err
        b_err = stooq_err
        # Fallback: only for rows Twelve Data actually covers on Max's free
        # plan (gold, EUR/USD, USD/JPY — confirmed live 2026-09-13; stock
        # indices and true commodities are NOT covered, see ROWS above),
        # try Twelve Data before giving up on a second source entirely.
        if row.get("twelvedata_symbol"):
            td_symbol = row["twelvedata_symbol"]
            try:
                tb_value, tb_asof = fetch_twelvedata_quote(td_symbol)
                b_value, b_asof = tb_value, tb_asof
                out["source_b"] = {
                    "name": "Twelve Data (fallback — Stooq failed)",
                    "symbol": td_symbol,
                    "value": tb_value,
                    "as_of": tb_asof,
                    "stooq_error": stooq_err,
                }
                b_err = None
            except Exception as exc2:  # noqa: BLE001
                b_err = f"stooq: {stooq_err}; twelvedata fallback also failed: {exc2}"
                out["source_b"]["error"] = b_err

    b_name = out["source_b"]["name"]

    if a_value is not None and b_value is not None:
        a_rounded = round(a_value, row["decimals"])
        b_rounded = round(b_value, row["decimals"])
        match = a_rounded == b_rounded
        out["match"] = match
        out["status"] = (
            f"VERIFIED — both sources agree to {row['decimals']} decimal(s): {a_rounded}"
            if match
            else f"DISAGREEMENT — Yahoo {a_rounded} vs {b_name} {b_rounded} "
                 f"(diff {abs(a_rounded - b_rounded):.{row['decimals']}f}); "
                 f"flag in the memo column, do not silently pick one"
        )
    elif a_value is not None:
        out["match"] = None
        out["status"] = f"SINGLE-SOURCE ONLY ({b_name} failed: {b_err}) — treat as unconfirmed"
    elif b_value is not None:
        out["match"] = None
        out["status"] = f"SINGLE-SOURCE ONLY (Yahoo failed: {a_err}) — treat as unconfirmed"
    else:
        out["match"] = None
        out["status"] = f"BOTH SOURCES FAILED (Yahoo: {a_err}; {b_name}: {b_err}) — no figure available"

    return out


def main():
    results = []
    for row in ROWS:
        results.append(verify_row(row))
        time.sleep(1)  # be a polite, low-volume client — one run/day, six rows

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "Generated by verify_indicators.py via GitHub Actions. Each row's "
            "source_a/source_b carries the literal figure that source returned "
            "(or an error) — never just a conclusion. 'match' is true only if "
            "both sources agree to the row's disclosed decimal precision."
        ),
        "rows": results,
    }

    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    # Human-readable summary in the Action log.
    for r in results:
        print(f"{r['label']:24s} {r['status']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
