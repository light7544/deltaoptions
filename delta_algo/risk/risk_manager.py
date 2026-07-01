"""
Risk Manager
============
Controls position sizing, circuit breakers, daily loss limits.
All risk checks are stateful and must be called before every trade.
"""
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Enforces all risk rules. Returns True/False on each check.
    Must be a singleton across the trading session.
    """

    def __init__(self, cfg: dict):
        r = cfg["risk"]
        self.max_risk_per_trade_pct = r["max_risk_per_trade_pct"]
        self.max_open_positions     = r["max_open_positions"]
        self.max_pos_per_underlying = r["max_positions_per_underlying"]
        self.max_daily_loss_pct     = r["max_daily_loss_pct"]
        self.max_consec_losses      = r["max_consecutive_losses"]
        self.iv_spike_threshold     = r["iv_spike_threshold"]
        self.halt_enabled           = r["halt_on_circuit_breaker"]
        self.resume_hours           = r["circuit_breaker_resume_hours"]
        self.sizing_method          = r.get("sizing_method", "portfolio_pct")

        # State
        self.daily_pnl: float = 0.0
        self.daily_pnl_date: date = date.today()
        self.consecutive_losses: int = 0
        self.halted: bool = False
        self.halt_time: Optional[datetime] = None
        self.open_positions: dict = {}   # symbol -> position dict
        self.trade_results: list = []    # list of PnL floats

    # ── State Management ─────────────────────────────────────

    def reset_daily_state(self):
        today = date.today()
        if self.daily_pnl_date != today:
            logger.info(f"New day {today}: resetting daily PnL (was {self.daily_pnl:.2f})")
            self.daily_pnl = 0.0
            self.daily_pnl_date = today

    def record_trade_result(self, pnl: float):
        """Call after each trade closes."""
        self.reset_daily_state()
        self.daily_pnl += pnl
        self.trade_results.append(pnl)
        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning(f"Loss recorded: {pnl:.2f} | Consecutive: {self.consecutive_losses}")
        else:
            self.consecutive_losses = 0
            logger.info(f"Win recorded: {pnl:.2f} | Streak reset")

    def register_position(self, symbol: str, data: dict):
        self.open_positions[symbol] = data

    def remove_position(self, symbol: str):
        self.open_positions.pop(symbol, None)

    # ── Circuit Breakers ─────────────────────────────────────

    def _check_circuit_breakers(self, portfolio_value: float) -> tuple:
        """Returns (allowed: bool, reason: str)."""
        self.reset_daily_state()

        # Auto-resume check
        if self.halted and self.halt_time:
            elapsed = (datetime.utcnow() - self.halt_time).total_seconds() / 3600
            if elapsed >= self.resume_hours:
                logger.info(f"Circuit breaker auto-resume after {elapsed:.1f}h")
                self.halted = False; self.halt_time = None
                self.consecutive_losses = 0

        if self.halted:
            return False, "circuit_breaker_active"

        # Daily loss limit
        if portfolio_value > 0:
            daily_loss_pct = abs(min(self.daily_pnl, 0)) / portfolio_value
            if daily_loss_pct >= self.max_daily_loss_pct:
                msg = f"Daily loss limit hit: {daily_loss_pct*100:.1f}% >= {self.max_daily_loss_pct*100:.1f}%"
                logger.error(msg)
                self._trigger_halt(msg)
                return False, "daily_loss_limit"

        # Consecutive losses
        if self.consecutive_losses >= self.max_consec_losses:
            msg = f"Max consecutive losses: {self.consecutive_losses}"
            logger.error(msg)
            self._trigger_halt(msg)
            return False, "consecutive_losses"

        return True, "ok"

    def _trigger_halt(self, reason: str):
        if self.halt_enabled:
            self.halted = True
            self.halt_time = datetime.utcnow()
            logger.error(f"🚨 CIRCUIT BREAKER TRIGGERED: {reason}")
            logger.error(f"   Trading halted for {self.resume_hours} hours")

    def check_iv_spike(self, current_iv: float, avg_iv: float) -> bool:
        """Returns False (block trade) if IV is spiking."""
        if avg_iv <= 0: return True
        ratio = current_iv / avg_iv
        if ratio >= self.iv_spike_threshold:
            logger.warning(f"IV spike detected: {current_iv:.3f} vs avg {avg_iv:.3f} (ratio {ratio:.2f})")
            return False
        return True

    # ── Position Checks ──────────────────────────────────────

    def check_max_positions(self, underlying: str = None) -> bool:
        total = len(self.open_positions)
        if total >= self.max_open_positions:
            logger.warning(f"Max open positions reached: {total}/{self.max_open_positions}")
            return False
        if underlying:
            per_und = sum(1 for k in self.open_positions if underlying in k)
            if per_und >= self.max_pos_per_underlying:
                logger.warning(f"Max positions for {underlying}: {per_und}/{self.max_pos_per_underlying}")
                return False
        return True

    # ── Position Sizing ──────────────────────────────────────

    def calculate_position_size(
        self,
        portfolio_value: float,
        max_loss_per_contract: float,
        min_size: int = 1,
        max_size: int = 100,
    ) -> int:
        """
        Calculate number of contracts based on risk per trade.
        max_loss_per_contract: the maximum $ loss if stop-loss is hit on one contract.
        """
        if max_loss_per_contract <= 0:
            return min_size

        risk_budget = portfolio_value * self.max_risk_per_trade_pct
        size = int(risk_budget / max_loss_per_contract)
        size = max(min_size, min(size, max_size))
        logger.info(
            f"Position size: {size} contracts | "
            f"risk_budget=${risk_budget:.2f} | "
            f"max_loss_per_contract=${max_loss_per_contract:.2f}"
        )
        return size

    # ── Master Pre-Trade Check ────────────────────────────────

    def pre_trade_check(
        self,
        portfolio_value: float,
        underlying: str = None,
        current_iv: float = None,
        avg_iv: float = None,
    ) -> tuple:
        """
        Run ALL risk checks before entering a trade.
        Returns (allowed: bool, reason: str)
        """
        # Circuit breakers
        ok, reason = self._check_circuit_breakers(portfolio_value)
        if not ok: return False, reason

        # Position limits
        if not self.check_max_positions(underlying):
            return False, "max_positions_exceeded"

        # IV spike
        if current_iv and avg_iv:
            if not self.check_iv_spike(current_iv, avg_iv):
                return False, "iv_spike"

        return True, "ok"

    def status_report(self) -> dict:
        return {
            "halted": self.halted,
            "halt_time": str(self.halt_time) if self.halt_time else None,
            "daily_pnl": round(self.daily_pnl, 4),
            "consecutive_losses": self.consecutive_losses,
            "open_positions": len(self.open_positions),
            "total_trades": len(self.trade_results),
            "win_rate": (
                sum(1 for p in self.trade_results if p > 0) / len(self.trade_results)
                if self.trade_results else 0
            ),
        }
