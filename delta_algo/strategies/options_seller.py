"""
strategies/options_seller.py
─────────────────────────────────────────────────────────────────
Options Selling Strategy Engine.

Supports:
  - Short Strangle  (sell OTM call + OTM put)
  - Short Straddle  (sell ATM call + ATM put)
  - Iron Condor     (short strangle + long wings for protection)
  - Credit Spread   (single direction vertical spread)

Entry Logic:
  - IV percentile filter
  - Delta-based strike selection
  - DTE filter
  - Trend/ATR regime filter

Exit Logic:
  - 35% profit target
  - 1.5x stop loss per leg
  - DTE-based auto-close
  - Daily loss limit
"""

import logging
import time
import uuid
from typing import Dict, List, Optional, Tuple

from api.client import DeltaClient, DeltaAPIError
from risk.manager import Leg, Position, RiskManager
from utils.helpers import (
    compute_iv_percentile,
    compute_sma,
    days_to_expiry,
    get_trend_direction,
    is_high_volatility_regime,
    is_within_trading_window,
    make_client_order_id,
    parse_expiry_from_symbol,
    parse_option_chain,
    select_strike_by_delta,
)

logger = logging.getLogger(__name__)


class OptionsSeller:
    """
    Core strategy engine for options selling.
    
    Usage:
        seller = OptionsSeller(client, risk_manager, config)
        signals = seller.scan_for_entry("BTC", expiry_date="04-04-2025")
        if signals:
            seller.enter_position(signals)
    """

    def __init__(
        self,
        client: DeltaClient,
        risk_manager: RiskManager,
        config: Dict,
    ):
        self.client = client
        self.rm = risk_manager
        self.strategy_cfg = config.get("strategy", {})
        self.entry_cfg = config.get("entry", {})
        self.exit_cfg = config.get("exit", {})
        self.risk_cfg = config.get("risk", {})
        self.live_cfg = config.get("live", {})

        self.strategy_name = self.strategy_cfg.get("name", "short_strangle")
        self.target_delta = self.strategy_cfg.get("target_delta", 0.15)
        self.delta_tolerance = self.strategy_cfg.get("delta_tolerance", 0.03)
        self.min_dte = self.strategy_cfg.get("min_dte", 3)
        self.max_dte = self.strategy_cfg.get("max_dte", 10)
        self.close_dte = self.strategy_cfg.get("close_dte", 1)
        self.min_iv_pct = self.strategy_cfg.get("min_iv_percentile", 30)
        self.max_iv_pct = self.strategy_cfg.get("max_iv_percentile", 95)
        self.profit_target_pct = self.exit_cfg.get("profit_target_pct", 0.35)
        self.sl_multiplier = self.exit_cfg.get("stop_loss_multiplier", 1.5)

        logger.info(f"OptionsSeller initialized | strategy={self.strategy_name}")

    # ══════════════════════════════════════════════════════════════════
    # MARKET SCANNING
    # ══════════════════════════════════════════════════════════════════

    def scan_for_entry(
        self,
        underlying: str,
        expiry_date: str,
        historical_ivs: List[float] = None,
        ohlc_candles: List[Dict] = None,
    ) -> Optional[Dict]:
        """
        Scan option chain for a valid entry.
        
        Returns entry signal dict if all filters pass, else None.
        
        Args:
            underlying:     e.g. 'BTC'
            expiry_date:    DD-MM-YYYY format
            historical_ivs: list of historical IV values for IV rank calc
            ohlc_candles:   OHLC candle data for trend filter
        """
        logger.info(f"Scanning {underlying} expiry={expiry_date}")

        # ── 1. Fetch option chain ────────────────────────────────────
        try:
            tickers = self.client.get_option_chain(underlying, expiry_date)
        except Exception as e:
            logger.error(f"Failed to fetch option chain: {e}")
            return None

        if not tickers:
            logger.warning(f"No options found for {underlying} {expiry_date}")
            return None

        chain = parse_option_chain(tickers)
        calls = chain["calls"]
        puts = chain["puts"]

        if not calls or not puts:
            logger.warning("Empty calls or puts chain")
            return None

        # ── 2. DTE filter ────────────────────────────────────────────
        sample = calls[0]
        dte = sample.get("dte", 0)
        if dte < self.min_dte or dte > self.max_dte:
            logger.info(f"DTE filter failed: dte={dte:.1f} (need {self.min_dte}-{self.max_dte})")
            return None

        # ── 3. IV Percentile filter ──────────────────────────────────
        # Use mid-chain ATM IV
        atm_call = min(calls, key=lambda x: abs(x["delta"] - 0.5)) if calls else None
        current_iv = atm_call["iv"] if atm_call else 0.0

        if historical_ivs and len(historical_ivs) > 10:
            iv_rank = compute_iv_percentile(current_iv, historical_ivs)
            if iv_rank < self.min_iv_pct or iv_rank > self.max_iv_pct:
                logger.info(
                    f"IV rank filter failed: rank={iv_rank:.1f} "
                    f"(need {self.min_iv_pct}-{self.max_iv_pct})"
                )
                return None
            logger.info(f"IV rank: {iv_rank:.1f}% ✅")
        else:
            iv_rank = 50.0  # default if no history

        # ── 4. Trend / Regime filter ─────────────────────────────────
        if self.strategy_cfg.get("trend_filter_enabled", True) and ohlc_candles:
            closes = [c["close"] for c in ohlc_candles if "close" in c]
            highs  = [c["high"]  for c in ohlc_candles if "high"  in c]
            lows   = [c["low"]   for c in ohlc_candles if "low"   in c]

            # Don't trade in extreme volatility regime
            if is_high_volatility_regime(
                highs, lows, closes,
                atr_period=self.strategy_cfg.get("atr_period", 14),
                multiplier=self.strategy_cfg.get("atr_regime_multiplier", 2.0),
            ):
                logger.info("High vol regime detected — skipping entry")
                return None

            # For non-directional strategies, neutral/slight trend is OK
            trend = get_trend_direction(
                closes,
                ma_period=self.strategy_cfg.get("trend_ma_period", 20),
            )
            logger.info(f"Trend direction: {trend}")
            # Skip if extreme trend (we'll be wrong on one side)
            # For Iron Condor, only trade in neutral/sideways
            if self.strategy_name == "iron_condor" and trend in ("up", "down"):
                logger.info(f"Iron Condor: skipping in {trend} trend")
                # Still allow but warn
        else:
            trend = "neutral"

        # ── 5. Select strikes ────────────────────────────────────────
        spot_price = calls[0].get("spot_price", 0) if calls else 0

        if self.strategy_name in ("short_strangle", "iron_condor"):
            return self._build_strangle_signal(
                calls, puts, dte, current_iv, iv_rank, spot_price, expiry_date, underlying
            )
        elif self.strategy_name == "short_straddle":
            return self._build_straddle_signal(
                calls, puts, dte, current_iv, iv_rank, spot_price, expiry_date, underlying
            )
        elif self.strategy_name == "credit_spread":
            return self._build_credit_spread_signal(
                calls, puts, dte, current_iv, iv_rank, spot_price, expiry_date, underlying
            )
        else:
            return self._build_strangle_signal(
                calls, puts, dte, current_iv, iv_rank, spot_price, expiry_date, underlying
            )

    def _build_strangle_signal(
        self, calls, puts, dte, iv, iv_rank, spot, expiry_date, underlying
    ) -> Optional[Dict]:
        """Build Short Strangle signal: sell OTM call + OTM put."""
        short_call = select_strike_by_delta(
            calls, self.target_delta, self.delta_tolerance
        )
        short_put = select_strike_by_delta(
            puts, -self.target_delta, self.delta_tolerance
        )

        if not short_call or not short_put:
            logger.warning("Could not select strikes for strangle")
            return None

        net_premium = short_call["mid"] + short_put["mid"]
        if net_premium <= 0:
            logger.warning("Net premium is zero — skipping")
            return None

        signal = {
            "strategy": "short_strangle",
            "underlying": underlying,
            "expiry_date": expiry_date,
            "dte": dte,
            "spot": spot,
            "iv": iv,
            "iv_rank": iv_rank,
            "net_premium": net_premium,
            "legs": [
                {
                    "type": "call",
                    "side": "sell",
                    "product_id": short_call["product_id"],
                    "symbol": short_call["symbol"],
                    "strike": short_call["strike"],
                    "delta": short_call["delta"],
                    "bid": short_call["bid"],
                    "ask": short_call["ask"],
                    "mid": short_call["mid"],
                },
                {
                    "type": "put",
                    "side": "sell",
                    "product_id": short_put["product_id"],
                    "symbol": short_put["symbol"],
                    "strike": short_put["strike"],
                    "delta": short_put["delta"],
                    "bid": short_put["bid"],
                    "ask": short_put["ask"],
                    "mid": short_put["mid"],
                },
            ],
        }

        if self.strategy_name == "iron_condor":
            signal = self._add_condor_wings(signal, calls, puts, spot)

        logger.info(
            f"Signal: {signal['strategy']} {underlying} "
            f"call_strike={short_call['strike']} put_strike={short_put['strike']} "
            f"net_premium={net_premium:.4f} iv_rank={iv_rank:.1f}%"
        )
        return signal

    def _add_condor_wings(
        self, signal: Dict, calls: List[Dict], puts: List[Dict], spot: float
    ) -> Dict:
        """Add protective wings to convert strangle to iron condor."""
        wing_pct = self.strategy_cfg.get("wing_width_pct", 0.05)
        short_call = next(
            (l for l in signal["legs"] if l["type"] == "call"), None
        )
        short_put = next(
            (l for l in signal["legs"] if l["type"] == "put"), None
        )

        if not short_call or not short_put:
            return signal

        # Long call wing (further OTM than short call)
        wing_call_strike = short_call["strike"] * (1 + wing_pct)
        long_call = min(
            [c for c in calls if c["strike"] > short_call["strike"]],
            key=lambda x: abs(x["strike"] - wing_call_strike),
            default=None,
        )

        # Long put wing (further OTM than short put)
        wing_put_strike = short_put["strike"] * (1 - wing_pct)
        long_put = min(
            [p for p in puts if p["strike"] < short_put["strike"]],
            key=lambda x: abs(x["strike"] - wing_put_strike),
            default=None,
        )

        if long_call:
            signal["legs"].append({
                "type": "call",
                "side": "buy",
                "product_id": long_call["product_id"],
                "symbol": long_call["symbol"],
                "strike": long_call["strike"],
                "delta": long_call["delta"],
                "bid": long_call["bid"],
                "ask": long_call["ask"],
                "mid": long_call["mid"],
            })
        if long_put:
            signal["legs"].append({
                "type": "put",
                "side": "buy",
                "product_id": long_put["product_id"],
                "symbol": long_put["symbol"],
                "strike": long_put["strike"],
                "delta": long_put["delta"],
                "bid": long_put["bid"],
                "ask": long_put["ask"],
                "mid": long_put["mid"],
            })

        signal["strategy"] = "iron_condor"
        # Adjust net premium (wings cost premium)
        wing_cost = 0
        if long_call:
            wing_cost += long_call["mid"]
        if long_put:
            wing_cost += long_put["mid"]
        signal["net_premium"] -= wing_cost
        return signal

    def _build_straddle_signal(
        self, calls, puts, dte, iv, iv_rank, spot, expiry_date, underlying
    ) -> Optional[Dict]:
        """Short Straddle: sell ATM call + ATM put at same strike."""
        # Select near-ATM (delta ~0.5 for call, ~-0.5 for put)
        atm_call = select_strike_by_delta(calls, 0.5, 0.1)
        atm_put  = select_strike_by_delta(puts, -0.5, 0.1)

        if not atm_call or not atm_put:
            return None

        net_premium = atm_call["mid"] + atm_put["mid"]
        return {
            "strategy": "short_straddle",
            "underlying": underlying,
            "expiry_date": expiry_date,
            "dte": dte, "spot": spot, "iv": iv, "iv_rank": iv_rank,
            "net_premium": net_premium,
            "legs": [
                {"type": "call", "side": "sell", "product_id": atm_call["product_id"],
                 "symbol": atm_call["symbol"], "strike": atm_call["strike"],
                 "bid": atm_call["bid"], "ask": atm_call["ask"], "mid": atm_call["mid"]},
                {"type": "put", "side": "sell", "product_id": atm_put["product_id"],
                 "symbol": atm_put["symbol"], "strike": atm_put["strike"],
                 "bid": atm_put["bid"], "ask": atm_put["ask"], "mid": atm_put["mid"]},
            ],
        }

    def _build_credit_spread_signal(
        self, calls, puts, dte, iv, iv_rank, spot, expiry_date, underlying
    ) -> Optional[Dict]:
        """Bull Put Credit Spread (default direction)."""
        short_put = select_strike_by_delta(puts, -self.target_delta, self.delta_tolerance)
        if not short_put:
            return None

        # Long put wing
        long_put = min(
            [p for p in puts if p["strike"] < short_put["strike"]],
            key=lambda x: abs(x["strike"] - short_put["strike"] * 0.95),
            default=None,
        )
        if not long_put:
            return None

        net_credit = short_put["mid"] - long_put["mid"]
        if net_credit <= 0:
            return None

        return {
            "strategy": "credit_spread",
            "underlying": underlying,
            "expiry_date": expiry_date,
            "dte": dte, "spot": spot, "iv": iv, "iv_rank": iv_rank,
            "net_premium": net_credit,
            "legs": [
                {"type": "put", "side": "sell", "product_id": short_put["product_id"],
                 "symbol": short_put["symbol"], "strike": short_put["strike"],
                 "bid": short_put["bid"], "ask": short_put["ask"], "mid": short_put["mid"]},
                {"type": "put", "side": "buy", "product_id": long_put["product_id"],
                 "symbol": long_put["symbol"], "strike": long_put["strike"],
                 "bid": long_put["bid"], "ask": long_put["ask"], "mid": long_put["mid"]},
            ],
        }

    # ══════════════════════════════════════════════════════════════════
    # ORDER EXECUTION
    # ══════════════════════════════════════════════════════════════════

    def enter_position(
        self,
        signal: Dict,
        available_balance: float,
    ) -> Optional[Position]:
        """
        Execute all legs of the signal.
        Returns Position object if successful.
        """
        position_id = str(uuid.uuid4())[:12]
        legs_placed = []

        for leg_sig in signal["legs"]:
            contracts = self.rm.calculate_contracts(
                available_balance=available_balance,
                premium_per_contract=leg_sig["mid"],
                stop_loss_multiplier=self.sl_multiplier,
            )
            if contracts == 0:
                logger.warning(f"Skipping leg {leg_sig['symbol']} — 0 contracts calculated")
                continue

            order = self._place_leg(leg_sig, contracts)
            if order:
                leg = Leg(
                    product_id=leg_sig["product_id"],
                    symbol=leg_sig["symbol"],
                    side=leg_sig["side"],
                    contracts=contracts,
                    entry_price=leg_sig["mid"],
                    current_price=leg_sig["mid"],
                    order_id=order.get("id"),
                    filled=(order.get("state") in ("open", "closed")),
                )
                legs_placed.append(leg)

        if not legs_placed:
            logger.error("No legs placed — aborting position")
            return None

        position = Position(
            position_id=position_id,
            strategy=signal["strategy"],
            underlying=signal["underlying"],
            legs=legs_placed,
        )
        self.rm.register_position(position)

        logger.info(
            f"✅ Position opened | id={position_id} strategy={signal['strategy']} "
            f"legs={len(legs_placed)} net_premium={signal['net_premium']:.4f}"
        )
        return position

    def _place_leg(self, leg_sig: Dict, contracts: int) -> Optional[Dict]:
        """Place a single option leg order."""
        order_type = self.entry_cfg.get("order_type", "limit_order")
        limit_offset = self.entry_cfg.get("limit_offset_pct", 0.005)

        # For sells: bid + small buffer. For buys: ask + small buffer
        if leg_sig["side"] == "sell":
            # Place slightly above bid to increase fill probability
            limit_price = leg_sig["bid"] * (1 + limit_offset)
        else:
            limit_price = leg_sig["ask"] * (1 - limit_offset)

        # Ensure minimum price
        limit_price = max(limit_price, 0.0001)

        coid = make_client_order_id(f"opt_{leg_sig['side'][:1]}")

        for attempt in range(1, self.entry_cfg.get("max_entry_attempts", 3) + 1):
            try:
                order = self.client.place_order(
                    product_id=leg_sig["product_id"],
                    size=contracts,
                    side=leg_sig["side"],
                    order_type=order_type,
                    limit_price=f"{limit_price:.6f}",
                    time_in_force=self.entry_cfg.get("time_in_force", "gtc"),
                    client_order_id=coid,
                )
                logger.info(
                    f"Leg placed | sym={leg_sig['symbol']} side={leg_sig['side']} "
                    f"size={contracts} price={limit_price:.6f} attempt={attempt}"
                )
                return order
            except DeltaAPIError as e:
                logger.error(f"Order failed (attempt {attempt}): {e}")
                if attempt < self.entry_cfg.get("max_entry_attempts", 3):
                    # Widen price slightly
                    if leg_sig["side"] == "sell":
                        limit_price *= 0.99  # lower sell price
                    else:
                        limit_price *= 1.01
                    time.sleep(1)
        return None

    # ══════════════════════════════════════════════════════════════════
    # EXIT MANAGEMENT
    # ══════════════════════════════════════════════════════════════════

    def update_position_prices(self, position: Position):
        """Fetch current market prices and update position legs."""
        for leg in position.legs:
            if leg.closed:
                continue
            try:
                ticker = self.client.get_ticker(leg.symbol)
                quotes = ticker.get("quotes", {})
                bid = float(quotes.get("best_bid", 0) or 0)
                ask = float(quotes.get("best_ask", 0) or 0)
                mid = (bid + ask) / 2 if bid and ask else float(ticker.get("mark_price", 0) or 0)
                leg.current_price = mid
            except Exception as e:
                logger.warning(f"Failed to update price for {leg.symbol}: {e}")

    def check_exits(self, position: Position) -> Optional[str]:
        """
        Check all exit conditions.
        Returns exit reason string or None.
        """
        if position.is_closed:
            return None

        # 1. Profit target
        if self.rm.check_profit_target(position, self.profit_target_pct):
            return "profit_target"

        # 2. Stop loss per leg
        for leg in position.legs:
            if not leg.closed and self.rm.check_stop_loss(leg, self.sl_multiplier):
                return f"stop_loss:{leg.symbol}"

        # 3. DTE auto-close
        if self.rm.check_dte_exit(position, self.close_dte):
            return "dte_expiry"

        return None

    def close_position(self, position: Position, reason: str) -> float:
        """
        Close all open legs of a position.
        Returns total realized PnL.
        """
        logger.info(f"Closing position | id={position.position_id} reason={reason}")
        position.status = "closing"
        total_pnl = 0.0

        for leg in position.legs:
            if leg.closed:
                continue
            close_price = self._close_leg(leg)
            leg.close_price = close_price
            leg.closed = True
            total_pnl += leg.realized_pnl

        position.status = "closed"
        position.closed_at = time.time()
        self.rm.close_position(position.position_id, total_pnl)

        logger.info(
            f"Position closed | id={position.position_id} "
            f"reason={reason} pnl={total_pnl:.4f}"
        )
        return total_pnl

    def _close_leg(self, leg: Leg) -> float:
        """Place closing order for a single leg. Returns fill price."""
        # Reverse the side
        close_side = "buy" if leg.side == "sell" else "sell"
        use_limit = self.exit_cfg.get("use_limit_on_exit", True)

        try:
            # First cancel any open order on this leg
            if leg.order_id:
                try:
                    self.client.cancel_order(leg.order_id, leg.product_id)
                except Exception:
                    pass

            if use_limit:
                # Try limit at current price
                order = self.client.place_order(
                    product_id=leg.product_id,
                    size=leg.contracts,
                    side=close_side,
                    order_type="limit_order",
                    limit_price=f"{leg.current_price:.6f}",
                    time_in_force="gtc",
                    reduce_only=True,
                    client_order_id=make_client_order_id("cls"),
                )
                # Wait for fill
                timeout = self.exit_cfg.get("limit_exit_timeout_seconds", 30)
                filled = self._wait_for_fill(order.get("id"), leg.product_id, timeout)
                if filled:
                    return leg.current_price
                # Fallback to market
                logger.warning(f"Limit exit timed out → switching to market")

            # Market order
            order = self.client.place_order(
                product_id=leg.product_id,
                size=leg.contracts,
                side=close_side,
                order_type="market_order",
                reduce_only=True,
                client_order_id=make_client_order_id("mkt"),
            )
            return leg.current_price  # Approximate at last known price

        except Exception as e:
            logger.error(f"Failed to close leg {leg.symbol}: {e}")
            return leg.current_price

    def _wait_for_fill(
        self, order_id: Optional[int], product_id: int, timeout: int = 30
    ) -> bool:
        """Poll order status until filled or timeout."""
        if not order_id:
            return False
        start = time.time()
        while time.time() - start < timeout:
            try:
                order = self.client.get_order_by_id(order_id)
                state = order.get("state", "")
                if state == "closed":
                    return True
                if state == "cancelled":
                    return False
            except Exception:
                pass
            time.sleep(2)
        return False

    # ══════════════════════════════════════════════════════════════════
    # EMERGENCY
    # ══════════════════════════════════════════════════════════════════

    def emergency_close_all(self):
        """Nuclear option: cancel all orders + close all positions."""
        logger.warning("🚨 EMERGENCY CLOSE ALL TRIGGERED")
        try:
            self.client.cancel_all_orders()
        except Exception as e:
            logger.error(f"Cancel all failed: {e}")

        for position in self.rm.get_open_positions():
            try:
                self.close_position(position, reason="emergency")
            except Exception as e:
                logger.error(f"Emergency close failed for {position.position_id}: {e}")
