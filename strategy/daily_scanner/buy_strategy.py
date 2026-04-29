from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEKLY_SCANNER_DIR = PROJECT_ROOT / "strategy" / "weekly_scanner"
if str(WEEKLY_SCANNER_DIR) not in sys.path:
    sys.path.append(str(WEEKLY_SCANNER_DIR))

from data_store import DB_PATH, get_connection, get_latest_trade_date, init_schema, upsert_daily_bars


WEEKLY_OUTPUT_FILE = PROJECT_ROOT / "output_files" / "weekly_analysis.xlsx"
DAILY_OUTPUT_FILE = PROJECT_ROOT / "output_files" / "daily_analysis.xlsx"
WEEKLY_SCAN_SHEET = "screened_stocks"
BUY_SIGNAL_SHEET = "buy_signal"
VOLUME_EMA_PERIOD = 50
VOLUME_EMA_MULTIPLIER = 1.5
INITIAL_FETCH_DAYS = 90
NTFY_TOPIC = "geet_send_buy_signal_to_iphone"
NTFY_TITLE = "Python Alert"


def load_weekly_candidates() -> list[str]:
    if not WEEKLY_OUTPUT_FILE.exists():
        return []
    try:
        scan_df = pd.read_excel(WEEKLY_OUTPUT_FILE, sheet_name=WEEKLY_SCAN_SHEET)
    except Exception:
        return []
    if "symbol" not in scan_df.columns:
        return []
    symbols = scan_df["symbol"].dropna().astype(str).str.strip().str.upper().unique().tolist()
    return [symbol for symbol in symbols if symbol]


def fetch_symbol_bars(symbol: str, start_date: datetime.date, end_date: datetime.date) -> list[tuple]:
    df = yf.download(
        f"{symbol}.NS",
        start=start_date.strftime("%Y-%m-%d"),
        end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if df is None or df.empty:
        return []

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    rows: list[tuple] = []
    for idx, row in df.iterrows():
        close_val = _to_float(row.get("Close"))
        if close_val is None:
            continue
        rows.append(
            (
                symbol,
                idx.strftime("%Y-%m-%d"),
                _to_float(row.get("Open")),
                _to_float(row.get("High")),
                _to_float(row.get("Low")),
                close_val,
                _to_float(row.get("Volume")),
                None,
            )
        )
    return rows


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").strip()
    if cleaned == "" or cleaned == "-" or cleaned.lower() == "nan":
        return None
    return float(cleaned)


def sync_latest_daily_bars(symbols: list[str]) -> None:
    today = datetime.now().date()
    with get_connection(DB_PATH) as conn:
        init_schema(conn)
        for symbol in symbols:
            latest_trade_date = get_latest_trade_date(conn, symbol)
            if latest_trade_date:
                start_date = datetime.strptime(latest_trade_date, "%Y-%m-%d").date() + timedelta(days=1)
            else:
                start_date = today - timedelta(days=INITIAL_FETCH_DAYS)

            if start_date > today:
                continue

            rows = fetch_symbol_bars(symbol, start_date, today)
            if rows:
                upsert_daily_bars(conn, rows)


def build_buy_signals(symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    placeholders = ",".join("?" for _ in symbols)
    query = f"""
        SELECT symbol, trade_date, open, close, volume
        FROM daily_bars
        WHERE symbol IN ({placeholders})
        ORDER BY symbol, trade_date
    """
    with get_connection(DB_PATH) as conn:
        bars_df = pd.read_sql_query(query, conn, params=symbols)

    if bars_df.empty:
        return pd.DataFrame()

    bars_df["volume_ema"] = bars_df.groupby("symbol")["volume"].transform(
        lambda s: s.ewm(span=VOLUME_EMA_PERIOD, adjust=False).mean()
    )
    latest_df = bars_df.sort_values("trade_date").groupby("symbol", as_index=False).tail(1).copy()
    latest_df["is_green"] = latest_df["close"] > latest_df["open"]
    latest_df["volume_ratio"] = latest_df["volume"] / latest_df["volume_ema"]

    buy_df = latest_df[
        (latest_df["is_green"]) & (latest_df["volume"] > (VOLUME_EMA_MULTIPLIER * latest_df["volume_ema"]))
    ].copy()
    if buy_df.empty:
        return buy_df

    buy_df = buy_df[
        [
            "symbol",
            "trade_date",
            "open",
            "close",
            "volume",
            "volume_ema",
            "volume_ratio",
        ]
    ].sort_values("symbol")
    buy_df["open"] = buy_df["open"].round(2)
    buy_df["close"] = buy_df["close"].round(2)
    buy_df["volume"] = buy_df["volume"].round(0)
    buy_df["volume_ema"] = buy_df["volume_ema"].round(0)
    buy_df["volume_ratio"] = buy_df["volume_ratio"].round(2)
    return buy_df


def save_buy_signals(output_path: Path, buy_df: pd.DataFrame) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            buy_df.to_excel(writer, sheet_name=BUY_SIGNAL_SHEET, index=False)
    else:
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="w") as writer:
            buy_df.to_excel(writer, sheet_name=BUY_SIGNAL_SHEET, index=False)


def send_buy_notifications(buy_df: pd.DataFrame) -> None:
    if buy_df.empty:
        return

    for symbol in buy_df["symbol"].dropna().astype(str):
        message = f"{symbol}: Buy signal detected."
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode(encoding="utf-8"),
                headers={"Title": NTFY_TITLE},
                timeout=10,
            )
            print(f"Notification sent for {symbol}")
        except Exception as exc:
            print(f"Failed to send notification for {symbol}: {exc}")


def main() -> None:
    symbols = load_weekly_candidates()
    if not symbols:
        print("No weekly screened stocks found. Run stock_scanner.py first.")
        save_buy_signals(DAILY_OUTPUT_FILE, pd.DataFrame())
        return

    sync_latest_daily_bars(symbols)
    buy_df = build_buy_signals(symbols)
    save_buy_signals(DAILY_OUTPUT_FILE, buy_df)
    send_buy_notifications(buy_df)

    print(f"Daily buy analysis saved to: {DAILY_OUTPUT_FILE}")
    print(f"Weekly candidates scanned: {len(symbols)}")
    print(f"Buy signals found: {len(buy_df)}")


if __name__ == "__main__":
    main()
