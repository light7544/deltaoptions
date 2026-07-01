"""
Live Trading Loop
=================
Main orchestrator: scan → check exits → enter → monitor.
Runs continuously with configurable scan intervals.
"""
import logging, signal, sys, time
from datetime import datetime

from api.client import DeltaClient
from risk.risk_manager import RiskManager
from strategies.strategy_factory import get_strategy
from utils.config_loader import load_config
from utils.logger import setup_logger
from utils.trade_logger import TradeLogger
from live.heartbeat import HeartbeatManager

logger = logging.getLogger(__name__)


class LiveTrader:
    def __init__(self, config_path: str = None):
        self.cfg = load_config(config_path)
        setup_logger("delta_algo", self.cfg.get("logging",{}))

        self.client       = DeltaClient.from_config(self.cfg)
        self.risk         = RiskManager(self.cfg)
        self.tlog         = TradeLogger(self.cfg["logging"]["trade_log_file"])
        self.strategy     = get_strategy(
            self.cfg["strategy"]["name"],
            self.client, self.cfg, self.risk, self.tlog
        )
        self.heartbeat    = HeartbeatManager(self.client, self.cfg)
        self.underlyings  = self.cfg["strategy"]["active_underlyings"]
        self.scan_interval= self.cfg["entry"]["scan_interval_seconds"]
        self.exit_interval= self.cfg["exit"]["close_check_interval_seconds"]
        self._running     = False
        self._last_scan   = 0
        self._last_exit   = 0

        # Graceful shutdown
        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def start(self):
        logger.info("=" * 60)
        logger.info(f"Delta Exchange Options Algo — LIVE TRADING")
        logger.info(f"Strategy: {self.cfg['strategy']['name']}")
        logger.info(f"Underlyings: {self.underlyings}")
        logger.info(f"Environment: {self.cfg['exchange']['environment']}")
        logger.info("=" * 60)

        # Connectivity check
        if not self.client.ping():
            logger.error("❌ API connection failed. Check credentials and network.")
            sys.exit(1)
        logger.info("✅ API connected")

        # Show account info
        try:
            profile = self.client.get_profile()
            logger.info(f"Account: {profile.get('email','?')} | "
                       f"KYC: {profile.get('is_kyc_done','?')}")
            balances = self.client.get_wallet_balances()
            for b in (balances if isinstance(balances,list) else []):
                sym = b.get("asset_symbol","")
                avail = b.get("available_balance","0")
                if float(avail or 0) > 0:
                    logger.info(f"  Balance [{sym}]: {avail}")
        except Exception as e:
            logger.warning(f"Could not fetch account info: {e}")

        # Start heartbeat (deadman switch)
        self.heartbeat.start()

        self._running = True
        logger.info("🟢 Live trading loop started")
        self._main_loop()

    def _main_loop(self):
        while self._running:
            now = time.time()
            try:
                # Exit checks (more frequent)
                if now - self._last_exit >= self.exit_interval:
                    self._run_exit_checks()
                    self._last_exit = now

                # Entry scans
                if now - self._last_scan >= self.scan_interval:
                    self._run_entry_scan()
                    self._last_scan = now

                # Risk status log every 5 min
                if int(now) % 300 < 5:
                    status = self.risk.status_report()
                    logger.info(f"RISK STATUS: {status}")

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)

            time.sleep(5)

    def _run_exit_checks(self):
        """Check all open positions for exit conditions."""
        try:
            positions = self.client.get_all_positions()
            exit_signals = self.strategy.check_exits(positions or [])
            for signal in exit_signals:
                reason = signal.get("reason","unknown")
                logger.info(f"EXIT SIGNAL: {signal['trade_id']} reason={reason}")
                result = self.strategy.execute_exit(signal, reason)
                logger.info(f"Exit result: {result}")
        except Exception as e:
            logger.error(f"Exit check loop error: {e}", exc_info=True)

    def _run_entry_scan(self):
        """Scan for new entry opportunities."""
        try:
            # Refresh risk state
            self.risk.reset_daily_state()

            balances = self.client.get_wallet_balances()
            pv = sum(float(b.get("available_balance","0") or 0)
                     for b in (balances if isinstance(balances, list) else []))
            pv = max(pv, 100)

            for underlying in self.underlyings:
                if not self._running: break

                # Quick pre-scan risk check
                allowed, reason = self.risk.pre_trade_check(pv, underlying)
                if not allowed:
                    logger.info(f"Scan skipped for {underlying}: {reason}")
                    continue

                logger.info(f"Scanning {underlying}...")
                setups = self.strategy.scan_entries(underlying)

                for setup in setups[:self.cfg["entry"]["max_new_trades_per_scan"]]:
                    result = self.strategy.execute_entry(setup)
                    logger.info(f"Entry result: {result.get('status')} | {result.get('trade_id')}")
                    time.sleep(1)  # small delay between entries

        except Exception as e:
            logger.error(f"Entry scan loop error: {e}", exc_info=True)

    def _handle_shutdown(self, signum, frame):
        logger.warning(f"Shutdown signal {signum} received")
        self._graceful_shutdown()

    def _graceful_shutdown(self):
        logger.warning("🔴 Initiating graceful shutdown...")
        self._running = False
        self.heartbeat.stop()

        # Cancel all open orders
        try:
            logger.warning("Cancelling all open orders...")
            self.client.cancel_all_orders()
            time.sleep(2)
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")

        logger.warning("Shutdown complete")
        sys.exit(0)

    def emergency_stop(self):
        """Nuclear option — cancel all orders AND close all positions."""
        logger.critical("🚨 EMERGENCY STOP ACTIVATED")
        self._running = False
        try:
            self.client.cancel_all_orders()
        except: pass
        try:
            self.client.close_all_positions()
        except: pass
        self.heartbeat.stop()
        sys.exit(1)
