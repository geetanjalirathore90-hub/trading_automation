from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "market_data.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS symbols_master (
            symbol TEXT PRIMARY KEY,
            company_name TEXT,
            industry TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            first_seen_date TEXT NOT NULL,
            last_seen_date TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_cap_snapshot (
            symbol TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            market_cap_inr REAL NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (symbol, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS daily_bars (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            volume REAL,
            turnover REAL,
            PRIMARY KEY (symbol, trade_date)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol_date
        ON daily_bars(symbol, trade_date);

        CREATE INDEX IF NOT EXISTS idx_market_cap_symbol_date
        ON market_cap_snapshot(symbol, snapshot_date);
        """
    )
    conn.commit()


def mark_all_symbols_inactive(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE symbols_master SET is_active = 0;")
    conn.commit()


def upsert_symbols(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO symbols_master(symbol, company_name, industry, is_active, first_seen_date, last_seen_date)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            company_name = excluded.company_name,
            industry = excluded.industry,
            is_active = 1,
            last_seen_date = excluded.last_seen_date
        """,
        rows,
    )
    conn.commit()


def upsert_market_caps(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO market_cap_snapshot(symbol, snapshot_date, market_cap_inr, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol, snapshot_date) DO UPDATE SET
            market_cap_inr = excluded.market_cap_inr,
            source = excluded.source
        """,
        rows,
    )
    conn.commit()


def upsert_daily_bars(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO daily_bars(symbol, trade_date, open, high, low, close, volume, turnover)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, trade_date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            turnover = excluded.turnover
        """,
        rows,
    )
    conn.commit()


def get_latest_market_caps(conn: sqlite3.Connection, symbols: Iterable[str]) -> dict[str, tuple[str, float]]:
    symbols_list = [sym for sym in symbols if sym]
    if not symbols_list:
        return {}

    placeholders = ",".join("?" for _ in symbols_list)
    query = f"""
        SELECT mcs.symbol, mcs.snapshot_date, mcs.market_cap_inr
        FROM market_cap_snapshot mcs
        JOIN (
            SELECT symbol, MAX(snapshot_date) AS max_snapshot_date
            FROM market_cap_snapshot
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        ) latest
            ON latest.symbol = mcs.symbol
           AND latest.max_snapshot_date = mcs.snapshot_date
    """
    rows = conn.execute(query, symbols_list).fetchall()
    return {symbol: (snapshot_date, market_cap) for symbol, snapshot_date, market_cap in rows}


def get_latest_trade_date(conn: sqlite3.Connection, symbol: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bars WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return row[0] if row and row[0] else None
