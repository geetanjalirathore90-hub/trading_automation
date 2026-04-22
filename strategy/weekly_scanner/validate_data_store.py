from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "market_data.db"


def print_summary(conn: sqlite3.Connection) -> None:
    symbol_count = conn.execute("SELECT COUNT(*) FROM symbols_master").fetchone()[0]
    active_symbol_count = conn.execute("SELECT COUNT(*) FROM symbols_master WHERE is_active = 1").fetchone()[0]
    cap_count = conn.execute("SELECT COUNT(*) FROM market_cap_snapshot").fetchone()[0]
    bars_count = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]

    print("=== DATASTORE SUMMARY ===")
    print(f"Database: {DB_PATH}")
    print(f"Total symbols in symbols_master: {symbol_count}")
    print(f"Active symbols: {active_symbol_count}")
    print(f"Market-cap snapshot rows: {cap_count}")
    print(f"Total daily_bars rows: {bars_count}")
    print()


def print_symbol_level_bars(conn: sqlite3.Connection, limit: int = 100) -> None:
    rows = conn.execute(
        """
        SELECT
            symbol,
            COUNT(*) AS bar_count,
            MIN(trade_date) AS first_trade_date,
            MAX(trade_date) AS latest_trade_date
        FROM daily_bars
        GROUP BY symbol
        ORDER BY bar_count DESC, symbol ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print("No rows in daily_bars yet.")
        return

    print("=== SYMBOL-LEVEL BARS (TOP BY ROW COUNT) ===")
    print("symbol | bar_count | first_trade_date | latest_trade_date")
    for symbol, bar_count, first_date, latest_date in rows:
        print(f"{symbol} | {bar_count} | {first_date} | {latest_date}")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Run sync_market_data.py first.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        print_summary(conn)
        print_symbol_level_bars(conn, limit=200)


if __name__ == "__main__":
    main()
