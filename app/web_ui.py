from pathlib import Path

import pandas as pd
import streamlit as st


DEFAULT_OUTPUT_FILE = "weekly_analysis.xlsx"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = PROJECT_ROOT / "output_files" / DEFAULT_OUTPUT_FILE
DAILY_OUTPUT_FILE = PROJECT_ROOT / "output_files" / "daily_analysis.xlsx"
TREND_SHEET = "market_trend"
SCAN_SHEET = "screened_stocks"
BUY_SIGNAL_SHEET = "buy_signal"


def _safe_read_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame()


def render_weekly_analysis(tab) -> None:
    with tab:
        st.subheader("Weekly Analysis")
        st.caption(f"Source file: {OUTPUT_FILE}")

        trend_df = _safe_read_sheet(OUTPUT_FILE, TREND_SHEET)
        scan_df = _safe_read_sheet(OUTPUT_FILE, SCAN_SHEET)

        st.markdown("### Market Trend")
        if trend_df.empty:
            st.info("No market trend data found. Run market_trend.py first.")
        else:
            trend_row = trend_df.iloc[0]
            col1, col2, col3 = st.columns(3)
            col1.metric("Daily EMA Direction", str(trend_row.get("daily_direction", "NA")))
            col2.metric("Weekly EMA Direction", str(trend_row.get("weekly_direction", "NA")))
            col3.metric("Overall Trend", str(trend_row.get("market_trend", "NA")))
            st.write(trend_df)

        st.markdown("### Stocks Near ATH")
        if scan_df.empty:
            st.info("No screened stock data found. Run stock_scanner.py first.")
        else:
            if "days_since_ath" in scan_df.columns:
                scan_df = scan_df[scan_df["days_since_ath"] >= 20].copy()
            st.dataframe(scan_df, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Momentum Trading", layout="wide")
    st.title("Momentum Trading Dashboard")

    weekly_tab, positions_tab, signals_tab = st.tabs(
        ["Weekly Analysis", "Positions", "Buy/Sell Signals"]
    )

    render_weekly_analysis(weekly_tab)

    with positions_tab:
        st.subheader("Positions")
        st.info("This tab is intentionally blank for now.")

    with signals_tab:
        st.subheader("Buy/Sell Signals")
        buy_df = _safe_read_sheet(DAILY_OUTPUT_FILE, BUY_SIGNAL_SHEET)
        st.markdown("### Buy Signal")
        if buy_df.empty:
            st.info("No buy signals found. Run daily_scanner/buy_strategy.py first.")
        else:
            st.dataframe(buy_df, use_container_width=True)


if __name__ == "__main__":
    main()
