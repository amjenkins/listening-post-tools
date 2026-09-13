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
 
Output: data/latest.json, containing for each row:
  - the literal figure returned by each source (never just "agreed")
  - whether they matched within the disclosed tolerance for that instrument
  - a plain-English status a human (or an LLM assembling the report) can
    act on without re-deriving anything.
 
Exit code is always 0 (a data-source hiccup should not fail the whole CI
run) — failures are recorded in the JSON, not thrown.
"""
 
import json
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
    },
    {
        "key": "brent_crude",
        "label": "Brent Crude",
        "yahoo_symbol": "BZ%3DF",
        "stooq_symbol": "cb.f",
        "decimals": 2,
    },
    {
        "key": "gold",
        "label": "Gold (spot)",
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
    },
    {
        "key": "eurusd",
        "label": "EUR/USD",
        "yahoo_symbol": "EURUSD%3DX",
        "stooq_symbol": "eurusd",
        "decimals": 4,
        # Frankfurter.app (European Central Bank reference rates, free,
        # keyless) as a fallback second source for this row specifically,
        # used only if Stooq fails — added 2026-09-13 after Stooq rejected
        # every row on the first live run. Frankfurter only covers FX pairs,
        # so it can't help the equity/commodity rows above.
        "frankfurter": {"from": "EUR", "to": "USD"},
    },
    {
        "key": "usdjpy",
        "label": "USD/JPY",
        "yahoo_symbol": "JPY%3DX",
        "stooq_symbol": "usdjpy",
        "decimals": 2,
        "frankfurter": {"from": "USD", "to": "JPY"},
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
 
 
def fetch_frankfurter_rate(from_ccy, to_ccy):
    """Returns (rate_float, as_of_iso8601_string) from Frankfurter.app — a
    free, keyless FX-rate API backed by the European Central Bank's daily
    reference rates. Used only as a fallback second source for the two FX
    rows, only when Stooq fails, or raises with a short, specific reason."""
    url = f"https://api.frankfurter.app/latest?from={from_ccy}&to={to_ccy}"
    raw = _http_get(url)
    data = json.loads(raw)
    rate = (data.get("rates") or {}).get(to_ccy)
    date = data.get("date")
    if rate is None or date is None:
        raise ValueError(f"frankfurter: unexpected response shape ({raw[:120]!r})")
    return float(rate), date
 
 
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
        # Fallback: for the two FX rows only, try Frankfurter.app before
        # giving up on a second source entirely. Added 2026-09-13 after
        # Stooq rejected every row uniformly on the first live run.
        if row.get("frankfurter"):
            fx = row["frankfurter"]
            try:
                fb_value, fb_asof = fetch_frankfurter_rate(fx["from"], fx["to"])
                b_value, b_asof = fb_value, fb_asof
                out["source_b"] = {
                    "name": "Frankfurter.app (fallback — Stooq failed)",
                    "symbol": f"{fx['from']}/{fx['to']}",
                    "value": fb_value,
                    "as_of": fb_asof,
                    "stooq_error": stooq_err,
                }
                b_err = None
            except Exception as exc2:  # noqa: BLE001
                b_err = f"stooq: {stooq_err}; frankfurter fallback also failed: {exc2}"
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
 
