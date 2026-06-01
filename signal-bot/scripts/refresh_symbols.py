"""Regenerate the US ticker whitelist used by X-bucket extraction.

Source: the official Nasdaq Trader symbol directory (authoritative for
NYSE + Nasdaq + NYSE American/Arca listings), two pipe-delimited files:
  - nasdaqlisted.txt  (Nasdaq-listed securities)
  - otherlisted.txt   (NYSE, NYSE American, NYSE Arca, BATS, IEX)

We keep only symbols that a $CASHTAG can actually produce — 1-5 letters with an
optional single-letter class suffix (e.g. BRK.A) — and drop test issues. The
result is written to ``process/us_tickers.txt`` (one symbol per line).

Refresh cadence: monthly is plenty (new listings/delistings are slow relative
to a daily attention signal). Run:

    python scripts/refresh_symbols.py

then commit the updated process/us_tickers.txt.
"""

import re
import sys
import urllib.request
from pathlib import Path

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

OUT_PATH = Path(__file__).resolve().parent.parent / "process" / "us_tickers.txt"

# Symbols traders cashtag heavily that are NOT in the equity directory:
# cash indices / volatility, and major crypto. Without these, $SPX and $VIX get
# wrongly dropped as fakes. Curated and small on purpose.
SUPPLEMENT = {
    # Index / volatility
    "SPX", "VIX", "NDX", "RUT", "DJI", "DJX", "XSP", "VVIX",
    # Major crypto (commonly cashtagged on X)
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "BNB",
}

# Same shape a $CASHTAG can yield in process/extract.py.
_CASHTAGGABLE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def _fetch(url: str) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "signal-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace").splitlines()


def _parse(lines: list[str], symbol_col: str, test_col: str) -> set[str]:
    if not lines:
        return set()
    header = lines[0].split("|")
    s_idx = header.index(symbol_col)
    t_idx = header.index(test_col) if test_col in header else -1
    out: set[str] = set()
    for line in lines[1:]:
        # The file ends with a "File Creation Time: ..." footer (no pipes).
        if "|" not in line:
            continue
        fields = line.split("|")
        if t_idx >= 0 and len(fields) > t_idx and fields[t_idx].strip() == "Y":
            continue
        symbol = fields[s_idx].strip().upper()
        if _CASHTAGGABLE.match(symbol):
            out.add(symbol)
    return out


def main() -> int:
    symbols: set[str] = set(SUPPLEMENT)
    symbols |= _parse(_fetch(NASDAQ_LISTED), "Symbol", "Test Issue")
    symbols |= _parse(_fetch(OTHER_LISTED), "ACT Symbol", "Test Issue")
    if len(symbols) < 1000:
        print(f"Refusing to write: only {len(symbols)} symbols parsed.", file=sys.stderr)
        return 1
    OUT_PATH.write_text("\n".join(sorted(symbols)) + "\n", encoding="utf-8")
    print(f"Wrote {len(symbols)} symbols to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
