"""Experiment engines endpoints — squeeze, spillover, revision."""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/experiments", tags=["experiments"])

_ENGINES = {"squeeze", "spillover", "revision"}


def _engine_module(engine: str):
    if engine == "squeeze":
        from services import squeeze_engine
        return squeeze_engine
    if engine == "spillover":
        from services import spillover_engine
        return spillover_engine
    if engine == "revision":
        from services import revision_engine
        return revision_engine
    raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")


@router.get("/status")
def all_status():
    """Status for all three experiment engines."""
    from services import squeeze_engine, spillover_engine, revision_engine
    return {
        "squeeze":   squeeze_engine.get_status(),
        "spillover": spillover_engine.get_status(),
        "revision":  revision_engine.get_status(),
    }


@router.get("/{engine}/positions")
def positions(engine: str, status: str = None):
    """Open + closed positions for the given engine."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    return mod.get_positions(status_filter=status)


@router.get("/{engine}/summary")
def summary(engine: str):
    """P&L, win rate, trade count, best/worst for the given engine."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    return mod.get_summary()


@router.post("/{engine}/run")
def manual_run(engine: str):
    """Trigger one scan cycle manually (ignores market-hours check)."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    # Reset last_scan_date so the scan actually runs
    if hasattr(mod, "_last_scan_date"):
        mod._last_scan_date = None
    result = mod.run_scan()
    return {"engine": engine, "result": result}


@router.post("/{engine}/close/{position_id}")
def close_position(engine: str, position_id: int):
    """Manually close a position by ID."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    return mod.close_position_by_id(position_id)


@router.get("/squeeze/universe")
def squeeze_universe():
    """
    Fetch the current high short-interest universe from HighShortInterest.com.
    Read-only — no trades. Use this to validate the dynamic universe before enabling it.
    """
    import re
    import httpx
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.current_row, self.in_td = [], [], False
        def handle_starttag(self, tag, attrs):
            if tag == "td": self.in_td = True
        def handle_endtag(self, tag):
            if tag == "td": self.in_td = False
            if tag == "tr":
                if self.current_row: self.rows.append(self.current_row[:])
                self.current_row = []
        def handle_data(self, data):
            if self.in_td:
                s = data.strip()
                if s: self.current_row.append(s)

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    stocks = {}

    try:
        for exchange, pages in [("Nasdaq", [1, 2]), ("NYSE", [1])]:
            for page in pages:
                url = f"https://www.highshortinterest.com/{exchange.lower()}/{page}/"
                resp = httpx.get(url, headers=headers, timeout=10, follow_redirects=True)
                resp.raise_for_status()
                p = _TableParser()
                p.feed(resp.text)
                for row in p.rows:
                    if (len(row) >= 4
                            and re.match(r"^[A-Z]{1,5}$", row[0])
                            and row[0] not in ("NYSE", "NASDAQ", "AMEX")):
                        ticker = row[0]
                        if ticker not in stocks:
                            stocks[ticker] = {
                                "ticker": ticker,
                                "company": row[1] if len(row) > 1 else "",
                                "exchange": exchange,
                                "short_float_pct": float(row[3].replace("%", "")) if len(row) > 3 else None,
                            }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HighShortInterest.com fetch failed: {e}")

    sorted_stocks = sorted(stocks.values(), key=lambda x: x["short_float_pct"] or 0, reverse=True)
    return {
        "source": "highshortinterest.com",
        "total": len(sorted_stocks),
        "note": "All stocks with >=20% short float. Sorted by short float desc.",
        "stocks": sorted_stocks,
    }
