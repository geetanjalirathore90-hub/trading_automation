from pathlib import Path

import pandas as pd

from data_store import DB_PATH, get_connection


DEFAULT_OUTPUT_FILE = "weekly_analysis.xlsx"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output_files"
SCREEN_THRESHOLD_RATIO = 0.9
SCREEN_SHEET_NAME = "screened_stocks"


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
