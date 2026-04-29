from pathlib import Path

import pandas as pd
import pandas_ta as ta

from data_store import DB_PATH, get_connection


DEFAULT_OUTPUT_FILE = "weekly_analysis.xlsx"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output_files"
SCREEN_THRESHOLD_RATIO = 0.90
SCREEN_SHEET_NAME = "screened_stocks"
SQUEEZE_MODE = "weekly"
SQUEEZE_MIN_HISTORY = 30


def scan_stocks() -> pd.DataFrame:
    query = """
        WITH latest_prices AS (
            SELECT
                d.symbol,
                d.close AS latest_close,
                d.high AS latest_high,
                d.trade_date AS latest_trade_date
            FROM daily_bars d
            JOIN (
                SELECT symbol, MAX(trade_date) AS max_trade_date
                FROM daily_bars
                GROUP BY symbol
            ) mx
                ON mx.symbol = d.symbol
               AND mx.max_trade_date = d.trade_date
        ),
        previous_all_time_highs AS (
            SELECT
                lp.symbol,
                MAX(db.high) AS all_time_high
            FROM latest_prices lp
            JOIN daily_bars db
                ON db.symbol = lp.symbol
               AND db.trade_date < lp.latest_trade_date
            GROUP BY lp.symbol
        ),
        previous_ath_dates AS (
            SELECT
                pah.symbol,
                MAX(db.trade_date) AS ath_trade_date
            FROM previous_all_time_highs pah
            JOIN daily_bars db
                ON db.symbol = pah.symbol
               AND db.high = pah.all_time_high
            GROUP BY pah.symbol
        )
        SELECT
            lp.symbol,
            lp.latest_trade_date,
            lp.latest_close,
            ath.all_time_high,
            ad.ath_trade_date,
            CAST(julianday(lp.latest_trade_date) - julianday(ad.ath_trade_date) AS INTEGER) AS days_since_ath
        FROM latest_prices lp
        JOIN previous_all_time_highs ath ON ath.symbol = lp.symbol
        JOIN previous_ath_dates ad ON ad.symbol = lp.symbol
        WHERE lp.latest_close >= (? * ath.all_time_high)
          AND lp.latest_high < ath.all_time_high
          AND CAST(julianday(lp.latest_trade_date) - julianday(ad.ath_trade_date) AS INTEGER) >= 20
        ORDER BY lp.symbol
    """

    with get_connection(DB_PATH) as conn:
        data = pd.read_sql_query(query, conn, params=(SCREEN_THRESHOLD_RATIO,))

    if data.empty:
        return data

    data = _apply_squeeze_filter(data)
    if data.empty:
        return data

    data["pct_below_ath"] = ((data["all_time_high"] - data["latest_close"]) / data["all_time_high"]) * 100
    data["ath_category"] = "recent_ath"
    data.loc[data["days_since_ath"] >= 50, "ath_category"] = "midrange_ath"
    data.loc[data["days_since_ath"] > 100, "ath_category"] = "distant_ath"

    data = data[data["days_since_ath"] >= 20].copy()
    data = data[
        (data["ath_category"] == "recent_ath")
        | (data["ath_category"] == "midrange_ath")
        | (data["ath_category"] == "distant_ath")
    ]

    data["latest_close"] = data["latest_close"].round(2)
    data["all_time_high"] = data["all_time_high"].round(2)
    data["pct_below_ath"] = data["pct_below_ath"].round(2)
    return data


def _apply_squeeze_filter(scan_df: pd.DataFrame) -> pd.DataFrame:
    if SQUEEZE_MODE == "weekly":
        return _filter_weekly_squeeze_candidates(scan_df)
    return _filter_squeeze_candidates(scan_df)


def _load_symbol_history(conn, symbol: str) -> pd.DataFrame:
    history_df = pd.read_sql_query(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE symbol = ?
        ORDER BY trade_date
        """,
        conn,
        params=(symbol,),
    )
    if history_df.empty:
        return history_df

    history_df["trade_date"] = pd.to_datetime(history_df["trade_date"])
    return history_df


def _extract_latest_squeeze_info(price_df: pd.DataFrame) -> dict[str, object] | None:
    if len(price_df) < SQUEEZE_MIN_HISTORY:
        return None

    squeeze_df = ta.squeeze(
        high=price_df["high"],
        low=price_df["low"],
        close=price_df["close"],
        asint=True,
    )
    if squeeze_df is None or squeeze_df.empty or "SQZ_ON" not in squeeze_df.columns:
        return None

    latest_sqz_on = squeeze_df["SQZ_ON"].iloc[-1]
    if int(latest_sqz_on) != 1:
        return None

    squeeze_value_col = next(
        (
            column
            for column in squeeze_df.columns
            if column.startswith("SQZ") and column not in {"SQZ_ON", "SQZ_OFF", "SQZ_NO"}
        ),
        None,
    )
    return {
        "sqz_on": int(latest_sqz_on),
        "sqz_value": float(squeeze_df[squeeze_value_col].iloc[-1]) if squeeze_value_col else None,
    }


def _filter_squeeze_candidates(scan_df: pd.DataFrame) -> pd.DataFrame:
    symbols = scan_df["symbol"].dropna().tolist()
    squeeze_rows: list[dict[str, object]] = []

    with get_connection(DB_PATH) as conn:
        for symbol in symbols:
            history_df = _load_symbol_history(conn, symbol)
            squeeze_info = _extract_latest_squeeze_info(history_df)
            if squeeze_info is None:
                continue

            squeeze_rows.append({"symbol": symbol, "squeeze_timeframe": "daily", **squeeze_info})

    if not squeeze_rows:
        return scan_df.iloc[0:0].copy()

    squeeze_flags_df = pd.DataFrame(squeeze_rows)
    merged = scan_df.merge(squeeze_flags_df, on="symbol", how="inner")
    return merged


def _filter_weekly_squeeze_candidates(scan_df: pd.DataFrame) -> pd.DataFrame:
    symbols = scan_df["symbol"].dropna().tolist()
    squeeze_rows: list[dict[str, object]] = []

    with get_connection(DB_PATH) as conn:
        for symbol in symbols:
            history_df = _load_symbol_history(conn, symbol)
            if history_df.empty:
                continue

            weekly_df = (
                history_df.set_index("trade_date")
                .resample("W-FRI")
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna(subset=["open", "high", "low", "close"])
                .reset_index()
            )
            squeeze_info = _extract_latest_squeeze_info(weekly_df)
            if squeeze_info is None:
                continue

            squeeze_rows.append({"symbol": symbol, "squeeze_timeframe": "weekly", **squeeze_info})

    if not squeeze_rows:
        return scan_df.iloc[0:0].copy()

    squeeze_flags_df = pd.DataFrame(squeeze_rows)
    merged = scan_df.merge(squeeze_flags_df, on="symbol", how="inner")
    return merged


def save_scan(output_path: Path, scan_df: pd.DataFrame) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            scan_df.to_excel(writer, sheet_name=SCREEN_SHEET_NAME, index=False)
    else:
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="w") as writer:
            scan_df.to_excel(writer, sheet_name=SCREEN_SHEET_NAME, index=False)


def main() -> None:
    output_file = OUTPUT_DIR / DEFAULT_OUTPUT_FILE
    scan_df = scan_stocks()
    save_scan(output_file, scan_df)
    print(f"Stock scan saved to: {output_file}")
    print(f"Screened symbols count: {len(scan_df)}")


if __name__ == "__main__":
    main()
