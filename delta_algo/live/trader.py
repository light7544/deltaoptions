"""
live/trader.py
─────────────────────────────────────────────────────────────────
Live Trading Loop.

Responsibilities:
  - Market data polling
  - Signal evaluation
  - Order placement and monitoring
  - Position PnL tracking
  - Heartbeat (Deadman Switch) maintenance
  - Trade logging
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from api.client import DeltaClient, DeltaAPIError
from risk.manager import RiskManager
from strategies.options_seller import OptionsSeller
from utils.helpers import (
    expiry_to_date_str,
    is_within_trading_window,
    now_utc,
    parse_expiry_from_symbol,
    days_to_expiry,
)

logger = logging.getLogger(__name__)


class LiveTrader:
    """
    Main live trading orchestrator.
    
    Usage:
        trader = LiveTrader(client, strategy, risk_manager, config)
        trader.run()   # blocks forever
    """

    def __init__(
        self,
        client: DeltaClient,
        strategy: OptionsSeller,
        risk_manager: RiskManager,
        config: Dict,
    ):
        self.client = client
        self.strategy = strategy
        self.rm = risk_manager
        self.config = config
        self.live_cfg = config.get("live", {})
        self.instrument_cfg = config.get("instruments", {})
        self.entry_cfg = config.get("entry", {})
        self.strategy_cfg = config.get("strategy", {})

        # Runtime state
        self._running = False
        self._heartbeat_id = f"delta_algo_{str(uuid.uuid4())[:8]}"
        self._last_heartbeat = 0.0
        self._last_position_sync = 0.0
        self._last_market_data = 0.0
        self._trades_today = 0
        self._today = now_utc().date()
        self._trade_log: List[Dict] = []

        # Trade log file
        log_dir = config.get("logging", {}).get("log_dir", "logs/")
        os.makedirs(log_dir, exist_ok=True)
        self._trade_log_path = os.path.join(log_dir, "trades.jsonl")

        logger.info(f"LiveTrader initialized | heartbeat_id={self._heartbeat_id}")

    # ══════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ══════════════════════════════════════════════════════════════════

    def run(self):
        """
        Main trading loop. Runs until interrupted.
        Call this from run_live.py
        """
        logger.info("🚀 Live trading loop starting...")
        self._running = True

        # Startup checks
        if not self._startup_checks():
            logger.error("Startup checks failed. Aborting.")
            return

        # Register deadman switch heartbeat
        if self.live_cfg.get("heartbeat_enabled", True):
            self._setup_heartbeat()

        poll_interval = self.live_cfg.get("poll_interval_seconds", 30)

        try:
            while self._running:
                loop_start = time.time()

                try:
                    self._main_iteration()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(f"Loop iteration error: {e}", exc_info=True)

                # Sleep for remaining interval
                elapsed = time.time() - loop_start
                sleep_for = max(0, poll_interval - elapsed)
                if sleep_for > 0:
                    time.sleep(sleep_for)

        except KeyboardInterrupt:
            logger.info("⏹  Shutdown requested by user")
        finally:
            self._shutdown()

    def _main_iteration(self):
        """One full cycle of the trading loop."""
        now = time.time()

        # ── Heartbeat ────────────────────────────────────────────────
        hb_interval = self.live_cfg.get("heartbeat_interval_seconds", 45)
        if self.live_cfg.get("heartbeat_enabled", True) and now - self._last_heartbeat >= hb_interval:
            self._send_heartbeat()

        # ── Daily reset ──────────────────────────────────────────────
        today = now_utc().date()
        if today != self._today:
            self._today = today
            self._trades_today = 0
            logger.info("Daily counters reset")

        # ── Fetch balance ─────────────────────────────────────────────
        try:
            available_balance = self.client.get_available_balance("USDT")
            total_balance = available_balance  # Simplified
            portfolio_margin = self.client.get_portfolio_margin("USDT")
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return

        # ── Circuit breaker checks ───────────────────────────────────
        if self.rm.is_halted():
            return

        if self.rm.check_daily_loss_limit(total_balance):
            logger.warning("Daily loss limit hit — no new entries")
            # Still monitor existing positions
            self._monitor_positions()
            return

        # ── Monitor existing positions ────────────────────────────────
        self._monitor_positions()

        # ── Evaluate entries ──────────────────────────────────────────
        in_window = is_within_trading_window(
            self.entry_cfg.get("entry_window_start", "09:00"),
            self.entry_cfg.get("entry_window_end",   "15:00"),
        )
        max_entries = self.entry_cfg.get("max_entries_per_day", 2)

        if in_window and self._trades_today < max_entries:
            allowed, reason = self.rm.check_entry_allowed(
                available_balance=available_balance,
                portfolio_margin=portfolio_margin,
                total_balance=total_balance,
                open_positions=len(self.rm.get_open_positions()),
            )
            if allowed:
                self._scan_and_enter(available_balance)
            else:
                logger.debug(f"Entry blocked: {reason}")

        # ── Log status ────────────────────────────────────────────────
        status = self.rm.get_status()
        logger.info(
            f"Status | balance={available_balance:.2f} "
            f"open_pos={status['open_positions']} "
            f"unreal_pnl={status['total_unrealized_pnl']:.2f} "
            f"trades_today={self._trades_today}"
        )

    # ══════════════════════════════════════════════════════════════════
    # ENTRY SCANNING
    # ══════════════════════════════════════════════════════════════════

    def _scan_and_enter(self, available_balance: float):
        """Scan all configured underlyings for entry signals."""
        underlyings = self.instrument_cfg.get("underlyings", [])

        for u_cfg in underlyings:
            if not u_cfg.get("enabled", True):
                continue

            underlying = u_cfg["symbol"]

            # Find suitable expiry
            expiry_date = self._find_best_expiry(underlying)
            if not expiry_date:
                logger.debug(f"No suitable expiry found for {underlying}")
                continue

            # Get OHLC for trend filter
            ohlc = self._get_ohlc_data(underlying)

            # Get historical IVs for IV rank
            hist_ivs = self._get_historical_ivs(underlying)

            # Scan for signal
            signal = self.strategy.scan_for_entry(
                underlying=underlying,
                expiry_date=expiry_date,
                historical_ivs=hist_ivs,
                ohlc_candles=ohlc,
            )

            if signal:
                logger.info(f"✅ Signal found for {underlying} — entering position")
                position = self.strategy.enter_position(signal, available_balance)
                if position:
                    self._trades_today += 1
                    self._log_trade({
                        "event": "entry",
                        "position_id": position.position_id,
                        "strategy": position.strategy,
                        "underlying": position.underlying,
                        "net_premium": signal.get("net_premium", 0),
                        "legs": len(position.legs),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                break  # One entry per iteration

    def _find_best_expiry(self, underlying: str) -> Optional[str]:
        """Find the best expiry date within DTE range."""
        min_dte = self.strategy_cfg.get("min_dte", 3)
        max_dte = self.strategy_cfg.get("max_dte", 10)

        try:
            # Fetch all live options products for this underlying
            products = self.client.get_products(
                contract_types="call_options",
                states="live",
            )

            # Filter for this underlying and DTE range
            valid_expiries = set()
            for p in products:
                if not p.get("symbol", "").startswith(f"C-{underlying}"):
                    continue
                expiry_code = parse_expiry_from_symbol(p["symbol"])
                if not expiry_code:
                    continue
                dte = days_to_expiry(expiry_code)
                if min_dte <= dte <= max_dte:
                    valid_expiries.add((dte, expiry_to_date_str(expiry_code)))

            if not valid_expiries:
                return None

            # Prefer expiry closest to middle of DTE range
            mid_dte = (min_dte + max_dte) / 2
            best = min(valid_expiries, key=lambda x: abs(x[0] - mid_dte))
            return best[1]  # DD-MM-YYYY format

        except Exception as e:
            logger.error(f"Error finding expiry for {underlying}: {e}")
            return None

    def _get_ohlc_data(self, underlying: str) -> List[Dict]:
        """Fetch OHLC candles for trend analysis."""
        symbol = f"{underlying}USD"  # e.g. BTCUSD
        try:
            candles = self.client.get_ohlc(symbol, resolution="1d")
            return candles
        except Exception as e:
            logger.warning(f"OHLC fetch failed for {symbol}: {e}")
            return []

    def _get_historical_ivs(self, underlying: str) -> List[float]:
        """
        Get historical IV values for IV rank calculation.
        Uses mark_vol from OHLC of ATM options as proxy.
        """
        lookback = self.strategy_cfg.get("iv_lookback_days", 30)
        try:
            # Get tickers for all options
            tickers = self.client.get_tickers(
                contract_types="call_options",
                underlying_asset_symbols=underlying,
            )
            # Extract IVs from mark_vol field
            ivs = []
            for t in tickers:
                mv = t.get("mark_vol")
                if mv:
                    try:
                        ivs.append(float(mv))
                    except (ValueError, TypeError):
                        pass
            return ivs[:lookback]
        except Exception as e:
            logger.warning(f"IV history fetch failed: {e}")
            return []

    # ══════════════════════════════════════════════════════════════════
    # POSITION MONITORING
    # ══════════════════════════════════════════════════════════════════

    def _monitor_positions(self):
        """Update prices and check exits for all open positions."""
        open_positions = self.rm.get_open_positions()

        for position in open_positions:
            if position.is_closed:
                continue

            # Update current prices
            self.strategy.update_position_prices(position)

            # Check exit conditions
            exit_reason = self.strategy.check_exits(position)
            if exit_reason:
                pnl = self.strategy.close_position(position, exit_reason)
                self._log_trade({
                    "event": "exit",
                    "position_id": position.position_id,
                    "strategy": position.strategy,
                    "underlying": position.underlying,
                    "pnl": round(pnl, 4),
                    "exit_reason": exit_reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                logger.info(
                    f"Position exited | id={position.position_id} "
                    f"reason={exit_reason} pnl={pnl:.4f}"
                )

    # ══════════════════════════════════════════════════════════════════
    # HEARTBEAT
    # ══════════════════════════════════════════════════════════════════

    def _setup_heartbeat(self):
        """Register the deadman switch heartbeat with Delta Exchange."""
        try:
            self.client.create_heartbeat(
                heartbeat_id=self._heartbeat_id,
                contract_types=["call_options", "put_options"],
            )
            self._last_heartbeat = time.time()
            logger.info(f"Deadman switch registered | id={self._heartbeat_id}")
        except Exception as e:
            logger.warning(f"Heartbeat setup failed: {e}")

    def _send_heartbeat(self):
        """Send periodic heartbeat acknowledgment."""
        ttl_ms = self.live_cfg.get("heartbeat_ttl_ms", 60000)
        try:
            self.client.send_heartbeat(self._heartbeat_id, ttl_ms)
            self._last_heartbeat = time.time()
            logger.debug(f"Heartbeat sent | id={self._heartbeat_id}")
        except Exception as e:
            logger.error(f"Heartbeat failed: {e}")

    # ══════════════════════════════════════════════════════════════════
    # STARTUP / SHUTDOWN
    # ══════════════════════════════════════════════════════════════════

    def _startup_checks(self) -> bool:
        """Verify API connectivity and account health."""
        logger.info("Running startup checks...")

        # 1. Connectivity
        if not self.client.ping():
            logger.error("Cannot reach Delta Exchange API")
            return False
        logger.info("✅ API connectivity OK")

        # 2. Account balance
        try:
            balance = self.client.get_available_balance("USDT")
            logger.info(f"✅ Account balance: {balance:.2f} USDT")
            if balance < 100:
                logger.warning(f"Low balance: {balance:.2f} USDT")
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return False

        # 3. Check no runaway positions
        try:
            positions = self.client.get_all_positions()
            logger.info(f"✅ Existing open positions: {len(positions)}")
        except Exception as e:
            logger.warning(f"Position check failed: {e}")

        return True

    def _shutdown(self):
        """Clean shutdown — cancel pending orders."""
        logger.info("Shutting down gracefully...")
        self._running = False

        # Final status
        status = self.rm.get_status()
        logger.info(f"Final status: {json.dumps(status)}")
        logger.info("🛑 Live trader stopped")

    # ══════════════════════════════════════════════════════════════════
    # TRADE LOGGING
    # ══════════════════════════════════════════════════════════════════

    def _log_trade(self, trade_data: Dict):
        """Append trade event to JSONL log file."""
        self._trade_log.append(trade_data)
        try:
            with open(self._trade_log_path, "a") as f:
                f.write(json.dumps(trade_data) + "\n")
        except Exception as e:
            logger.error(f"Trade log write failed: {e}")

    def get_trade_history(self) -> List[Dict]:
        return self._trade_log.copy()
