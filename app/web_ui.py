from pathlib import Path

import pandas as pd
import streamlit as st


DEFAULT_OUTPUT_FILE = "weekly_analysis.xlsx"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = PROJECT_ROOT / "output_files" / DEFAULT_OUTPUT_FILE
TREND_SHEET = "market_trend"
SCAN_SHEET = "screened_stocks"


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

        st.markdown("### Screened Stocks (Close vs ATH)")
        if scan_df.empty:
            st.info("No screened stock data found. Run stock_scanner.py first.")
        else:
            if "ath_category" not in scan_df.columns:
                st.warning("ath_category not found in screened data. Please rerun stock_scanner.py.")
                st.dataframe(scan_df, use_container_width=True)
                return

            recent_df = scan_df[scan_df["ath_category"] == "recent_ath"]
            midrange_df = scan_df[scan_df["ath_category"] == "midrange_ath"]
            distant_df = scan_df[scan_df["ath_category"] == "distant_ath"]

            st.markdown("#### Recent ATH (days since ATH: 20 to 49)")
            if recent_df.empty:
                st.info("No stocks in recent ATH bucket.")
            else:
                st.dataframe(recent_df, use_container_width=True)

            st.markdown("#### Midrange ATH (days since ATH: 50 to 100)")
            if midrange_df.empty:
                st.info("No stocks in midrange ATH bucket.")
            else:
                st.dataframe(midrange_df, use_container_width=True)

            st.markdown("#### Distant ATH (days since ATH: > 100)")
            if distant_df.empty:
                st.info("No stocks in distant ATH bucket.")
            else:
                st.dataframe(distant_df, use_container_width=True)


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
        st.info("This tab is intentionally blank for now.")


if __name__ == "__main__":
    main()
