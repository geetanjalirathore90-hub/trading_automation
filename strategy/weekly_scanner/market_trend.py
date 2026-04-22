from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

import pandas as pd
import requests


DEFAULT_INDEX_SYMBOL = "NIFTY MIDSMALLCAP 400"
DEFAULT_OUTPUT_FILE = "weekly_analysis.xlsx"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output_files"
NSE_BASE_URL = "https://www.nseindia.com"
NSE_HISTORY_API = "https://www.nseindia.com/api/historicalOR/indicesHistory"


@dataclass
class TrendResult:
    index_symbol: str
    as_of_date: str
    daily_ema_10: float
    daily_ema_10_prev_6: float
    weekly_ema_10: float
    weekly_ema_10_prev_3: float
    daily_direction: str
    weekly_direction: str
    market_trend: str
    notes: str


def _direction(current: float, previous: float) -> str:
    if current > previous:
        return "UP"
    if current < previous:
        return "DOWN"
    return "FLAT"


def _direction_from_consecutive_values(value_series: pd.Series, comparisons: int) -> str:
    required_points = comparisons + 1
    if len(value_series) < required_points:
        raise ValueError(
            f"Not enough data to evaluate {comparisons} consecutive comparisons. "
            f"Need at least {required_points} closing values."
        )

    recent = value_series.tail(required_points).reset_index(drop=True)
    print(recent)
    is_rising = all(recent.iloc[-i] > recent.iloc[-i - 1] for i in range(1, len(recent)))
    if is_rising:
        return "UP"

    is_falling = all(recent.iloc[-i] < recent.iloc[-i - 1] for i in range(1, len(recent)))
    if is_falling:
        return "DOWN"

    return "FLAT"


def _classify_market_trend(weekly_direction: str, daily_direction: str) -> Tuple[str, str]:
    if weekly_direction == "UP" and daily_direction == "UP":
        return "UP", "Both weekly and daily EMA-direction checks are rising."
    if weekly_direction == "UP" and daily_direction == "DOWN":
        return "PREDOMINANT_TREND_UP_SHORT_TERM_DOWN", "Weekly EMA direction rising, daily EMA direction falling."
    if weekly_direction == "DOWN" and daily_direction == "UP":
        return "PREDOMINANT_TREND_DOWN_SHORT_TERM_UP", "Weekly EMA direction falling, daily EMA direction rising."
    if weekly_direction == "DOWN" and daily_direction == "DOWN":
        return "BEAR_MARKET", "Both weekly and daily EMA-direction checks are falling."
    return "MIXED_OR_FLAT", "At least one EMA-direction check is flat or mixed."


def fetch_index_data(index_symbol: str) -> pd.DataFrame:
    to_date = datetime.now()
    from_date = to_date - timedelta(days=550)

    params = {
        "indexType": index_symbol,
        "from": from_date.strftime("%d-%m-%Y"),
        "to": to_date.strftime("%d-%m-%Y"),
    }
    headers = {
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

    session = requests.Session()
    session.headers.update(headers)

    # Prime NSE cookies before calling API endpoint.
    session.get(NSE_BASE_URL, timeout=15)
    # NSE historical API is most reliable with smaller windows.
    records = []
    window_start = from_date
    while window_start <= to_date:
        window_end = min(window_start + timedelta(days=90), to_date)
        window_params = {
            **params,
            "from": window_start.strftime("%d-%m-%Y"),
            "to": window_end.strftime("%d-%m-%Y"),
        }
        response = session.get(NSE_HISTORY_API, params=window_params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        records.extend(payload.get("data", []))
        window_start = window_end + timedelta(days=1)

    if not records:
        raise ValueError(
            f"No market data returned from NSE for index '{index_symbol}'. "
            "Please verify the index name supported by NSE historical API."
        )

    rows = []
    for rec in records:
        date_raw = rec.get("EOD_TIMESTAMP")
        close_raw = rec.get("EOD_CLOSE_INDEX_VAL")
        if date_raw is None or close_raw is None:
            continue
        rows.append(
            {
                "Date": pd.to_datetime(str(date_raw).title(), format="%d-%b-%Y", errors="coerce"),
                "Close": float(str(close_raw).replace(",", "")),
            }
        )

    data = pd.DataFrame(rows).dropna(subset=["Date", "Close"]).sort_values("Date")
    if data.empty:
        raise ValueError("NSE response received, but no valid Date/Close rows were parsed.")

    data = data.set_index("Date")
    return data[["Close"]].dropna().copy()


def calculate_trend(close_df: pd.DataFrame, index_symbol: str) -> TrendResult:
    if len(close_df) < 6:
        raise ValueError("Not enough daily data to evaluate 5 consecutive daily EMA comparisons.")

    daily_ema = close_df["Close"].ewm(span=10, adjust=False).mean()
    daily_current = float(daily_ema.iloc[-1])
    daily_prev = float(daily_ema.iloc[-6])
    daily_direction = _direction_from_consecutive_values(daily_ema, comparisons=5)

    weekly_close = close_df["Close"].resample("W-FRI").last().dropna()
    if len(weekly_close) < 4:
        raise ValueError("Not enough weekly data to evaluate 3 consecutive weekly EMA comparisons.")

    weekly_ema = weekly_close.ewm(span=10, adjust=False).mean()
    weekly_current = float(weekly_ema.iloc[-1])
    weekly_prev = float(weekly_ema.iloc[-3])
    weekly_direction = _direction_from_consecutive_values(weekly_ema, comparisons=3)

    market_trend, notes = _classify_market_trend(weekly_direction, daily_direction)

    return TrendResult(
        index_symbol=index_symbol,
        as_of_date=close_df.index[-1].strftime("%Y-%m-%d"),
        daily_ema_10=round(daily_current, 4),
        daily_ema_10_prev_6=round(daily_prev, 4),
        weekly_ema_10=round(weekly_current, 4),
        weekly_ema_10_prev_3=round(weekly_prev, 4),
        daily_direction=daily_direction,
        weekly_direction=weekly_direction,
        market_trend=market_trend,
        notes=notes,
    )


def save_to_excel(result: TrendResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    row = pd.DataFrame([result.__dict__])

    if output_path.exists():
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            row.to_excel(writer, sheet_name="market_trend", index=False)
    else:
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="w") as writer:
            row.to_excel(writer, sheet_name="market_trend", index=False)


def main() -> None:
    index_symbol = DEFAULT_INDEX_SYMBOL
    output_file = OUTPUT_DIR / DEFAULT_OUTPUT_FILE

    close_df = fetch_index_data(index_symbol=index_symbol)
    result = calculate_trend(close_df=close_df, index_symbol=index_symbol)
    save_to_excel(result=result, output_path=output_file)

    print("Weekly market trend analysis complete.")
    print(f"Symbol: {result.index_symbol}")
    print(f"As of: {result.as_of_date}")
    print(f"Daily direction (5 consecutive EMA checks): {result.daily_direction}")
    print(f"Weekly direction (3 consecutive EMA checks): {result.weekly_direction}")
    print(f"Market trend: {result.market_trend}")
    print(f"Saved to: {output_file}")


if __name__ == "__main__":
    main()
