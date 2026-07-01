"""
risk/manager.py
─────────────────────────────────────────────────────────────────
Risk Management Engine.

Responsibilities:
  - Track open positions and their PnL
  - Enforce max risk per trade
  - Circuit breakers (consecutive losses, daily loss, IV spike)
  - Margin utilization checks
  - Daily PnL tracking
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Leg:
    """A single option leg (one side of a strangle/condor)."""
    product_id: int
    symbol: str
    side: str               # 'buy' or 'sell'
    contracts: int
    entry_price: float      # premium collected/paid per contract
    current_price: float = 0.0
    order_id: Optional[int] = None
    filled: bool = False
    closed: bool = False
    close_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        if self.side == "sell":
            return (self.entry_price - self.current_price) * self.contracts
        else:
            return (self.current_price - self.entry_price) * self.contracts

    @property
    def realized_pnl(self) -> float:
        if not self.closed:
            return 0.0
        if self.side == "sell":
            return (self.entry_price - self.close_price) * self.contracts
        else:
            return (self.close_price - self.entry_price) * self.contracts

    @property
    def stop_loss_price(self) -> float:
        """1.5x the entry premium is the stop loss level."""
        return self.entry_price * 1.5  # configurable via risk config


@dataclass
class Position:
    """A full options position (e.g., Short Strangle = 2 legs)."""
    position_id: str
    strategy: str           # 'short_strangle' | 'iron_condor' etc.
    underlying: str
    legs: List[Leg] = field(default_factory=list)
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    status: str = "open"    # 'open' | 'closing' | 'closed'

    @property
    def net_premium(self) -> float:
        """Total net premium collected (positive = credit received)."""
        total = 0.0
        for leg in self.legs:
            if leg.side == "sell":
                total += leg.entry_price * leg.contracts
            else:
                total -= leg.entry_price * leg.contracts
        return total

    @property
    def unrealized_pnl(self) -> float:
        return sum(leg.unrealized_pnl for leg in self.legs)

    @property
    def realized_pnl(self) -> float:
        return sum(leg.realized_pnl for leg in self.legs)

    @property
    def pnl_pct(self) -> float:
        """PnL as % of initial premium."""
        if self.net_premium == 0:
            return 0.0
        return self.unrealized_pnl / abs(self.net_premium) * 100

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    def update_leg_price(self, product_id: int, current_price: float):
        for leg in self.legs:
            if leg.product_id == product_id:
                leg.current_price = current_price


# ══════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ══════════════════════════════════════════════════════════════════════

class RiskManager:
    """
    Central risk management engine.
    
    Usage:
        rm = RiskManager(risk_cfg)
        rm.check_entry_allowed(balance, positions)
        rm.check_stop_loss(position)
        rm.record_trade_result(won=False)
    """

    def __init__(self, risk_cfg: Dict):
        self.cfg = risk_cfg
        cb = risk_cfg.get("circuit_breaker", {})

        # Limits
        self.max_risk_per_trade_pct = risk_cfg.get("max_risk_per_trade_pct", 0.01)
        self.max_open_positions = risk_cfg.get("max_open_positions", 4)
        self.max_contracts_per_leg = risk_cfg.get("max_contracts_per_leg", 5)
        self.margin_utilization_max = risk_cfg.get("margin_utilization_max", 0.60)

        # Circuit breaker settings
        self.max_consecutive_losses = cb.get("max_consecutive_losses", 3)
        self.max_daily_loss_pct = cb.get("max_daily_loss_pct", 0.03)
        self.iv_spike_threshold = cb.get("iv_spike_threshold", 1.5)
        self.halt_duration_hours = cb.get("halt_duration_hours", 4)

        # State tracking
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
        self._daily_start_balance = 0.0
        self._last_day = datetime.now(timezone.utc).date()
        self._halted_until: Optional[float] = None
        self._positions: Dict[str, Position] = {}

        logger.info("RiskManager initialized")

    # ── Day Reset ──────────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = datetime.now(timezone.utc).date()
        if today != self._last_day:
            logger.info(f"Daily reset | prev PnL={self._daily_pnl:.2f}")
            self._daily_pnl = 0.0
            self._last_day = today

    # ── Circuit Breaker ────────────────────────────────────────────────

    def is_halted(self) -> bool:
        """Check if trading is halted by circuit breaker."""
        if self._halted_until is None:
            return False
        if time.time() < self._halted_until:
            remaining = (self._halted_until - time.time()) / 3600
            logger.warning(f"Trading HALTED — resumes in {remaining:.1f}h")
            return True
        else:
            self._halted_until = None
            self._consecutive_losses = 0
            logger.info("Circuit breaker reset. Trading resumed.")
            return False

    def _trigger_halt(self, reason: str):
        duration = self.halt_duration_hours * 3600
        self._halted_until = time.time() + duration
        logger.error(
            f"🚨 CIRCUIT BREAKER TRIGGERED: {reason}. "
            f"Trading halted for {self.halt_duration_hours}h"
        )

    def record_trade_result(self, won: bool, pnl: float = 0.0):
        """Call after each position closes."""
        self._reset_daily_if_needed()
        self._daily_pnl += pnl

        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            logger.warning(
                f"Loss recorded. Consecutive losses: {self._consecutive_losses}"
            )

        if self._consecutive_losses >= self.max_consecutive_losses:
            self._trigger_halt(
                f"{self._consecutive_losses} consecutive losses"
            )

    def check_daily_loss_limit(self, current_balance: float) -> bool:
        """
        Returns True if daily loss limit breached.
        Call this each loop iteration.
        """
        self._reset_daily_if_needed()
        if self._daily_start_balance <= 0:
            self._daily_start_balance = current_balance
            return False

        daily_loss_pct = self._daily_pnl / self._daily_start_balance
        if daily_loss_pct < -self.max_daily_loss_pct:
            self._trigger_halt(
                f"Daily loss {daily_loss_pct*100:.2f}% > "
                f"{self.max_daily_loss_pct*100:.2f}% limit"
            )
            return True
        return False

    def check_iv_spike(
        self,
        current_iv: float,
        avg_iv: float,
    ) -> bool:
        """Returns True if IV is spiking above threshold → halt."""
        if avg_iv <= 0:
            return False
        ratio = current_iv / avg_iv
        if ratio > self.iv_spike_threshold:
            self._trigger_halt(
                f"IV spike: current={current_iv:.2f} avg={avg_iv:.2f} "
                f"ratio={ratio:.2f} > {self.iv_spike_threshold}"
            )
            return True
        return False

    # ── Entry Checks ──────────────────────────────────────────────────

    def check_entry_allowed(
        self,
        available_balance: float,
        portfolio_margin: float,
        total_balance: float,
        open_positions: int,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Run all pre-trade risk checks.
        """
        if self.is_halted():
            return False, "Circuit breaker active"

        if open_positions >= self.max_open_positions:
            return False, f"Max positions reached ({open_positions}/{self.max_open_positions})"

        if total_balance > 0:
            margin_util = portfolio_margin / total_balance
            if margin_util > self.margin_utilization_max:
                return False, (
                    f"Margin utilization {margin_util:.1%} > "
                    f"{self.margin_utilization_max:.1%} max"
                )

        if available_balance <= 0:
            return False, "No available balance"

        return True, "OK"

    # ── Exit Checks ───────────────────────────────────────────────────

    def check_profit_target(
        self,
        position: Position,
        profit_target_pct: float = 0.35,
    ) -> bool:
        """Return True if position has hit profit target."""
        if position.net_premium == 0:
            return False
        pct = position.unrealized_pnl / abs(position.net_premium)
        if pct >= profit_target_pct:
            logger.info(
                f"Profit target hit | pos={position.position_id} "
                f"pnl={pct*100:.1f}%"
            )
            return True
        return False

    def check_stop_loss(
        self,
        leg: Leg,
        stop_loss_multiplier: float = 1.5,
    ) -> bool:
        """Return True if a leg has hit its stop loss."""
        if leg.entry_price <= 0:
            return False
        sl_price = leg.entry_price * stop_loss_multiplier
        if leg.side == "sell" and leg.current_price >= sl_price:
            logger.warning(
                f"Stop loss hit | sym={leg.symbol} "
                f"entry={leg.entry_price:.4f} current={leg.current_price:.4f} "
                f"sl={sl_price:.4f}"
            )
            return True
        return False

    def check_dte_exit(self, position: Position, close_dte: float = 1.0) -> bool:
        """Return True if any leg is within close_dte days of expiry."""
        for leg in position.legs:
            from utils.helpers import parse_expiry_from_symbol, days_to_expiry
            exp = parse_expiry_from_symbol(leg.symbol)
            if exp:
                dte = days_to_expiry(exp)
                if dte <= close_dte:
                    logger.info(
                        f"DTE exit triggered | sym={leg.symbol} dte={dte:.2f}"
                    )
                    return True
        return False

    # ── Position Registry ─────────────────────────────────────────────

    def register_position(self, position: Position):
        self._positions[position.position_id] = position
        logger.info(f"Position registered | id={position.position_id}")

    def get_position(self, position_id: str) -> Optional[Position]:
        return self._positions.get(position_id)

    def get_open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if not p.is_closed]

    def close_position(self, position_id: str, pnl: float):
        pos = self._positions.get(position_id)
        if pos:
            pos.status = "closed"
            pos.closed_at = time.time()
            won = pnl >= 0
            self.record_trade_result(won=won, pnl=pnl)
            logger.info(
                f"Position closed | id={position_id} pnl={pnl:.2f} won={won}"
            )

    def get_total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.get_open_positions())

    # ── Sizing ────────────────────────────────────────────────────────

    def calculate_contracts(
        self,
        available_balance: float,
        premium_per_contract: float,
        stop_loss_multiplier: float = 1.5,
    ) -> int:
        """
        How many contracts can we trade given risk limits?
        """
        from utils.helpers import calculate_position_size
        return calculate_position_size(
            available_balance=available_balance,
            max_risk_pct=self.max_risk_per_trade_pct,
            premium_per_contract=premium_per_contract,
            stop_loss_multiplier=stop_loss_multiplier,
            max_contracts=self.max_contracts_per_leg,
        )

    # ── Status Report ─────────────────────────────────────────────────

    def get_status(self) -> Dict:
        open_pos = self.get_open_positions()
        return {
            "halted": self.is_halted(),
            "consecutive_losses": self._consecutive_losses,
            "daily_pnl": round(self._daily_pnl, 4),
            "open_positions": len(open_pos),
            "total_unrealized_pnl": round(self.get_total_unrealized_pnl(), 4),
        }
