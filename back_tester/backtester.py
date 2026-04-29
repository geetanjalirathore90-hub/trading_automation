from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from base_strategy import BaseStrategy
from sell_strategy import DualStopSellStrategy
from paths import BACKTEST_OUTPUT_FILE, DB_PATH
from data_store import get_connection


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    start_date: str                         # "YYYY-MM-DD"
    end_date: str                           # "YYYY-MM-DD"
    initial_capital: float = 1_000_000.0   # INR
    risk_pct_per_trade: float = 0.01        # 1% of capital risked per trade
    max_open_positions: int = 10            # concurrent position cap
    data_source: str = "sqlite"             # "sqlite" or "yfinance"
    output_path: Path = field(default_factory=lambda: BACKTEST_OUTPUT_FILE)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    initial_sl: float
    exit_reason: str        # "SELL_SIGNAL" | "END_OF_DATA"

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Runs any pair of (buy_strategy, sell_strategy) over historical data.

    The engine:
      - Knows nothing about HOW signals are generated.
      - Calls buy_strategy.generate_signals(df) → df with `signal` col.
      - Calls sell_strategy.generate_signals(df) → overlays SELL signals.
      - Simulates trades: enter at NEXT bar's open after BUY signal.
      - Exits at NEXT bar's open after SELL signal, or end of data.
      - Sizes positions via sell_strategy.compute_position_size().
      - Tracks running capital and concurrent position limit.
    """

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        buy_strategy: BaseStrategy,
        sell_strategy: DualStopSellStrategy,
        symbols: list[str],
    ) -> dict:
        trades: list[Trade] = []
        equity_curve: list[dict] = []
        capital = self.config.initial_capital

        for symbol in symbols:
            df = self._load_data(symbol)
            if df is None or df.empty:
                print(f"  {symbol}: no data, skipping")
                continue

            df = self._slice_dates(df)
            if len(df) < 60:   # need enough bars for 50D EMA to stabilise
                print(f"  {symbol}: insufficient bars ({len(df)}), skipping")
                continue

            df = buy_strategy.generate_signals(df)
            df = sell_strategy.generate_signals(df)
            symbol_trades = self._simulate(symbol, df, capital, sell_strategy)

            for t in symbol_trades:
                capital += t.pnl
                trades.append(t)
                equity_curve.append({
                    "date": t.exit_date,
                    "symbol": t.symbol,
                    "pnl": t.pnl,
                    "capital_after": round(capital, 2),
                })

            print(f"  {symbol}: {len(symbol_trades)} trades")

        metrics = self._compute_metrics(trades, equity_curve)
        self._save_results(trades, equity_curve, metrics)
        return metrics

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_data(self, symbol: str) -> Optional[pd.DataFrame]:
        if self.config.data_source == "sqlite":
            return self._load_from_sqlite(symbol)
        return self._load_from_yfinance(symbol)

    def _load_from_sqlite(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            with get_connection(DB_PATH) as conn:
                df = pd.read_sql_query(
                    """
                    SELECT trade_date, open, high, low, close, volume
                    FROM daily_bars
                    WHERE symbol = ?
                    ORDER BY trade_date
                    """,
                    conn,
                    params=(symbol,),
                )
            if df.empty:
                return None
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            return df
        except Exception as exc:
            print(f"  {symbol}: SQLite error — {exc}")
            return None

    def _load_from_yfinance(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            raw = yf.download(
                f"{symbol}.NS",
                start=self.config.start_date,
                end=self.config.end_date,
                interval="1d",
                auto_adjust=False,
                progress=False,
            )
            if raw is None or raw.empty:
                return None
            if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
                raw.columns = raw.columns.get_level_values(0)
            raw = raw.reset_index().rename(columns={
                "Date": "trade_date",
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            return raw[["trade_date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        except Exception as exc:
            print(f"  {symbol}: yfinance error — {exc}")
            return None

    def _slice_dates(self, df: pd.DataFrame) -> pd.DataFrame:
        start = pd.to_datetime(self.config.start_date)
        end = pd.to_datetime(self.config.end_date)
        return df[(df["trade_date"] >= start) & (df["trade_date"] <= end)].reset_index(drop=True)

    # ------------------------------------------------------------------
    # Trade simulation
    # ------------------------------------------------------------------

    def _simulate(
        self,
        symbol: str,
        df: pd.DataFrame,
        capital: float,
        sell_strategy: DualStopSellStrategy,
    ) -> list[Trade]:
        trades: list[Trade] = []
        in_position = False
        entry_price = 0.0
        entry_date = ""
        shares = 0
        initial_sl = 0.0

        for i in range(len(df) - 1):   # -1 because we enter/exit at i+1 open
            row = df.iloc[i]
            next_row = df.iloc[i + 1]
            signal = row.get("signal", "HOLD")

            if not in_position:
                if signal == "BUY":
                    entry_price = float(next_row["open"])   # realistic: next bar open
                    entry_date = str(next_row["trade_date"])[:10]

                    # Recompute initial SL at entry price
                    sl_from_swing = float(row.get("swing_low_ref", entry_price * 0.88) or entry_price * 0.88)
                    sl_from_swing *= (1 - sell_strategy.sl_swing_buffer)
                    sl_from_max = entry_price * (1 - sell_strategy.sl_max_loss)
                    initial_sl = max(sl_from_swing, sl_from_max)

                    shares = sell_strategy.compute_position_size(
                        entry_price=entry_price,
                        initial_sl=initial_sl,
                        total_capital=capital,
                        risk_pct=self.config.risk_pct_per_trade,
                    )

                    if shares <= 0:
                        continue   # position sizing failed (e.g. SL too close)

                    in_position = True

            else:
                if signal == "SELL":
                    exit_price = float(next_row["open"])
                    exit_date = str(next_row["trade_date"])[:10]
                    trades.append(Trade(
                        symbol=symbol,
                        entry_date=entry_date,
                        exit_date=exit_date,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        shares=shares,
                        initial_sl=initial_sl,
                        exit_reason="SELL_SIGNAL",
                    ))
                    capital += trades[-1].pnl
                    in_position = False

        # Close any open position at last bar's close (end of data)
        if in_position:
            last = df.iloc[-1]
            trades.append(Trade(
                symbol=symbol,
                entry_date=entry_date,
                exit_date=str(last["trade_date"])[:10],
                entry_price=entry_price,
                exit_price=float(last["close"]),
                shares=shares,
                initial_sl=initial_sl,
                exit_reason="END_OF_DATA",
            ))

        return trades

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, trades: list[Trade], equity_curve: list[dict]) -> dict:
        if not trades:
            return {"error": "No trades executed"}

        pnls = [t.pnl for t in trades]
        winners = [t for t in trades if t.is_winner]
        losers = [t for t in trades if not t.is_winner]

        total_return = sum(pnls)
        win_rate = len(winners) / len(trades) * 100
        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else 0
        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

        # Max drawdown from equity curve
        max_drawdown = self._max_drawdown(equity_curve)

        # Sharpe ratio (annualised, assumes ~252 trading days)
        sharpe = self._sharpe(equity_curve)

        return {
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate_pct": round(win_rate, 2),
            "total_pnl_inr": round(total_return, 2),
            "avg_win_inr": round(avg_win, 2),
            "avg_loss_inr": round(avg_loss, 2),
            "expectancy_inr": round(expectancy, 2),
            "max_drawdown_inr": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 3),
            "final_capital": round(self.config.initial_capital + total_return, 2),
            "return_pct": round(total_return / self.config.initial_capital * 100, 2),
        }

    @staticmethod
    def _max_drawdown(equity_curve: list[dict]) -> float:
        if not equity_curve:
            return 0.0
        capitals = [e["capital_after"] for e in equity_curve]
        peak = capitals[0]
        max_dd = 0.0
        for c in capitals:
            peak = max(peak, c)
            dd = peak - c
            max_dd = max(max_dd, dd)
        return max_dd

    @staticmethod
    def _sharpe(equity_curve: list[dict], risk_free_rate: float = 0.065) -> float:
        """Annualised Sharpe using daily P&L returns."""
        if len(equity_curve) < 2:
            return 0.0
        pnls = pd.Series([e["pnl"] for e in equity_curve])
        daily_rf = risk_free_rate / 252
        excess = pnls - daily_rf
        if excess.std() == 0:
            return 0.0
        return float((excess.mean() / excess.std()) * (252 ** 0.5))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _save_results(
        self,
        trades: list[Trade],
        equity_curve: list[dict],
        metrics: dict,
    ) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)

        trade_rows = [
            {
                "symbol": t.symbol,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "entry_price": round(t.entry_price, 2),
                "exit_price": round(t.exit_price, 2),
                "shares": t.shares,
                "initial_sl": round(t.initial_sl, 2),
                "pnl_inr": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 2),
                "winner": "YES" if t.is_winner else "NO",
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ]

        trades_df = pd.DataFrame(trade_rows)
        equity_df = pd.DataFrame(equity_curve)
        metrics_df = pd.DataFrame([metrics])

        path = self.config.output_path
        mode = "a" if path.exists() else "w"
        kw = {"if_sheet_exists": "replace"} if mode == "a" else {}

        with pd.ExcelWriter(path, engine="openpyxl", mode=mode, **kw) as writer:
            metrics_df.to_excel(writer, sheet_name="bt_summary", index=False)
            trades_df.to_excel(writer, sheet_name="bt_trade_log", index=False)
            equity_df.to_excel(writer, sheet_name="bt_equity_curve", index=False)

        print(f"\nBacktest results saved to: {path}")


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_backtest(
    symbols: list[str],
    start_date: str = "2022-01-01",
    end_date: str | None = None,
    initial_capital: float = 1_000_000.0,
    data_source: str = "sqlite",
) -> dict:
    from buy_strategy import VolumeGreenCandleStrategy

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        risk_pct_per_trade=0.01,
        data_source=data_source,
    )

    engine = BacktestEngine(config)
    buy_strat = VolumeGreenCandleStrategy()
    sell_strat = DualStopSellStrategy()

    print(f"\nRunning backtest: {start_date} → {end_date}")
    print(f"Symbols: {len(symbols)}  |  Capital: ₹{initial_capital:,.0f}  |  Source: {data_source}\n")

    metrics = engine.run(buy_strat, sell_strat, symbols)

    print("\n=== BACKTEST RESULTS ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    return metrics


if __name__ == "__main__":
    # Default: load symbols from weekly scan, backtest from SQLite
    try:
        from buy_strategy import load_weekly_candidates
        symbols = load_weekly_candidates()
    except Exception:
        symbols = []

    if not symbols:
        print("No symbols found. Run stock_scanner.py first, or pass symbols manually.")
        print("Example: edit this block to symbols = ['RELIANCE', 'INFY', ...]")
    else:
        run_backtest(
            symbols=symbols,
            start_date="2022-01-01",
            data_source="sqlite",
        )