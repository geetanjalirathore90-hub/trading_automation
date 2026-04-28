from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
import yfinance as yf

from data_store import (
    DB_PATH,
    PROJECT_ROOT,
    get_connection,
    get_latest_market_caps,
    get_latest_trade_date,
    init_schema,
    mark_all_symbols_inactive,
    upsert_daily_bars,
    upsert_market_caps,
    upsert_symbols,
)


MIN_MARKET_CAP_INR = 5_000_000_000
MAX_MARKET_CAP_INR = 1_000_000_000_000
BATCH_SIZE = 50
API_SLEEP_SECONDS = 0.2

NSE_HOME = "https://www.nseindia.com"
NSE_EQ_LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftymidsmallcap400list.csv"
MARKET_CAP_CSV_PATH = PROJECT_ROOT / "data" / "market_cap_snapshot.csv"
MAX_HISTORY_WINDOW_DAYS = 365*3
MARKET_CAP_STALENESS_DAYS = 30


@dataclass
class SymbolRecord:
    symbol: str
    company_name: str
    industry: str


def _nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    session.get(NSE_HOME, timeout=15)
    return session


def fetch_symbols_from_nse(session: requests.Session) -> list[SymbolRecord]:
    response = session.get(NSE_EQ_LIST_URL, timeout=20)
    response.raise_for_status()
    decoded = response.content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(decoded))

    symbols: list[SymbolRecord] = []
    for row in reader:
        symbol = (row.get(" Symbol") or row.get("Symbol") or "").strip().upper()
        name = (row.get(" Company Name") or row.get("Company Name") or "").strip()
        industry = (row.get(" Industry") or row.get("Industry") or "").strip()
        if not symbol:
            continue
        symbols.append(SymbolRecord(symbol=symbol, company_name=name, industry=industry))
    return symbols


def load_market_cap_snapshot(symbols: list[SymbolRecord]) -> dict[str, float]:
    market_caps: dict[str, float] = {}
    for symbol_record in symbols:
        symbol = symbol_record.symbol
        symbol_ns = symbol + ".NS"
        stock = yf.Ticker(symbol_ns)
        market_cap = stock.info.get("marketCap")
        if market_cap is None:
            continue
        market_caps[symbol] = market_cap
        time.sleep(API_SLEEP_SECONDS)
    return market_caps


def _is_snapshot_stale(snapshot_date: str, threshold_date: datetime.date) -> bool:
    try:
        snapshot_dt = datetime.strptime(snapshot_date, "%Y-%m-%d").date()
    except ValueError:
        return True
    return snapshot_dt < threshold_date


def resolve_market_caps(
    conn,
    symbols: list[SymbolRecord],
    today: datetime.date,
) -> tuple[dict[str, float], list[tuple[str, str, float, str]], int]:
    latest_caps = get_latest_market_caps(conn, (record.symbol for record in symbols))
    threshold_date = today - timedelta(days=MARKET_CAP_STALENESS_DAYS)

    symbols_to_refresh: list[SymbolRecord] = []
    market_caps: dict[str, float] = {}
    for record in symbols:
        latest = latest_caps.get(record.symbol)
        if latest is None:
            symbols_to_refresh.append(record)
            continue

        snapshot_date, cached_cap = latest
        if _is_snapshot_stale(snapshot_date=snapshot_date, threshold_date=threshold_date):
            symbols_to_refresh.append(record)
            continue
        market_caps[record.symbol] = cached_cap

    refreshed_caps = load_market_cap_snapshot(symbols_to_refresh) if symbols_to_refresh else {}
    market_caps.update(refreshed_caps)

    snapshot_date = today.strftime("%Y-%m-%d")
    cap_rows = [
        (symbol, snapshot_date, cap, "yfinance")
        for symbol, cap in refreshed_caps.items()
    ]
    return market_caps, cap_rows, len(symbols_to_refresh)


def eligible_symbols(symbols: Iterable[SymbolRecord], market_caps: dict[str, float]) -> list[SymbolRecord]:
    selected: list[SymbolRecord] = []
    for rec in symbols:
        cap = market_caps.get(rec.symbol)
        if cap is None:
            continue
        if MIN_MARKET_CAP_INR <= cap <= MAX_MARKET_CAP_INR:
            selected.append(rec)
    return selected


def _history_date_window(last_date: str | None) -> tuple[str, str]:
    today = datetime.now().date()
    if last_date:
        start = datetime.strptime(last_date, "%Y-%m-%d").date() + timedelta(days=1)
    else:
        start = today - timedelta(days=MAX_HISTORY_WINDOW_DAYS)
    return start.strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y")


def fetch_equity_history(
    session: requests.Session,
    symbol: str,
    from_date: str,
    to_date: str,
) -> list[tuple]:
    del session  # price history now comes from yfinance

    start = datetime.strptime(from_date, "%d-%m-%Y").date()
    end = datetime.strptime(to_date, "%d-%m-%Y").date()
    ticker = f"{symbol}.NS"
    history_df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if history_df is None or history_df.empty:
        return []

    # yfinance can return MultiIndex columns even for a single ticker.
    if hasattr(history_df.columns, "nlevels") and history_df.columns.nlevels > 1:
        history_df.columns = history_df.columns.get_level_values(0)

    parsed: list[tuple] = []
    for idx, row in history_df.iterrows():
        parsed.append(
            (
                symbol,
                idx.strftime("%Y-%m-%d"),
                _to_float(row.get("Open")),
                _to_float(row.get("High")),
                _to_float(row.get("Low")),
                _to_float(row.get("Close")),
                _to_float(row.get("Volume")),
                None,
            )
        )
    return parsed


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").strip()
    if cleaned == "" or cleaned == "-":
        return None
    return float(cleaned)


def main() -> None:
    session = _nse_session()
    all_symbols = fetch_symbols_from_nse(session)
    today = datetime.now().date()
    snapshot_date = today.strftime("%Y-%m-%d")
    with get_connection(DB_PATH) as conn:
        init_schema(conn)
        mark_all_symbols_inactive(conn)

        market_caps, cap_rows, refreshed_count = resolve_market_caps(
            conn=conn,
            symbols=all_symbols,
            today=today,
        )
        eligible = eligible_symbols(all_symbols, market_caps)

        symbol_rows = [
            (s.symbol, s.company_name, s.industry, snapshot_date, snapshot_date)
            for s in all_symbols
        ]
        upsert_symbols(conn, symbol_rows)

        if cap_rows:
            upsert_market_caps(conn, cap_rows)

        for i in range(0, len(eligible), BATCH_SIZE):
            batch = eligible[i : i + BATCH_SIZE]
            bar_rows: list[tuple] = []
            for sym in batch:
                latest_date = get_latest_trade_date(conn, sym.symbol)
                from_date, to_date = _history_date_window(latest_date)
                from_dt = datetime.strptime(from_date, "%d-%m-%Y").date()
                to_dt = datetime.strptime(to_date, "%d-%m-%Y").date()
                if from_dt > to_dt:
                    continue
                try:
                    bars = fetch_equity_history(session, sym.symbol, from_date, to_date)
                    bar_rows.extend(bars)
                    print(f"{sym.symbol}: fetched {len(bars)} bars")
                except Exception as exc:
                    print(f"Skipping {sym.symbol}: {exc}")
                time.sleep(API_SLEEP_SECONDS)

            if bar_rows:
                upsert_daily_bars(conn, bar_rows)
            print(f"Processed batch {i // BATCH_SIZE + 1}: {len(batch)} symbols")

    print("Local datastore sync complete.")
    print(f"Total symbols snapshot: {len(all_symbols)}")
    print(f"Market-cap refreshed symbols (> {MARKET_CAP_STALENESS_DAYS} days old): {refreshed_count}")
    print(f"Eligible symbols by market cap: {len(eligible)}")
    print(f"Database path: {DB_PATH}")
    print(f"Market-cap source CSV: {MARKET_CAP_CSV_PATH}")


if __name__ == "__main__":
    main()
