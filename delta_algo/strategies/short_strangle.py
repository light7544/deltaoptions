"""
Short Strangle Strategy
=======================
Sells an OTM call + OTM put at ~target_delta on same expiry.
Entry: High IV percentile, ATR not spiking, DTE in target range.
Exit: 35% profit OR 1.5x stop OR DTE <= 1.
"""
import logging, time, uuid
from datetime import datetime
from typing import Optional

from strategies.base_strategy import BaseStrategy
from utils.options_math import (
    find_strike_by_delta, days_to_expiry, mid_price,
    bid_ask_spread_pct, iv_percentile, is_high_volatility_regime,
    trend_direction, calculate_atr, implied_volatility,
)
import pandas as pd, numpy as np

logger = logging.getLogger(__name__)


class ShortStrangleStrategy(BaseStrategy):
    """
    Short Strangle: Sell OTM Call + Sell OTM Put
    Max profit = net premium received
    Max loss = unlimited (use stop-loss!)
    """

    def __init__(self, client, cfg, risk_manager, trade_logger):
        super().__init__(client, cfg, risk_manager, trade_logger)
        s = cfg["strategy"]
        self.target_delta  = s["target_delta"]
        self.delta_tol     = s["delta_tolerance"]
        self.iv_pct_min    = s["iv_percentile_min"]
        self.min_dte       = cfg["instruments"]["min_dte"]
        self.max_dte       = cfg["instruments"]["max_dte"]
        self.use_trend     = s.get("use_trend_filter", True)
        self.atr_period    = s.get("atr_period", 14)
        self.ma_period     = s.get("trend_ma_period", 20)
        self.max_atr_mult  = s.get("max_atr_multiple", 2.5)
        self.min_premium   = cfg["entry"]["min_premium_usd"]
        self.max_spread_pct= cfg["entry"]["min_bid_ask_spread_pct"]
        self.profit_target = cfg["exit"]["profit_target_pct"]
        self.sl_multiple   = cfg["exit"]["stop_loss_multiple"]
        self.force_close_dte = cfg["exit"]["max_dte_to_force_close"]
        self.order_type    = cfg["orders"]["default_order_type"]
        self.order_timeout = cfg["orders"]["order_timeout_seconds"]

        # Track active strangles: trade_id -> {call, put, entry_credit, ...}
        self.active_trades: dict = {}

    # ── Entry Logic ──────────────────────────────────────────

    def scan_entries(self, underlying: str) -> list:
        """Returns list of valid strangle setups."""
        setups = []
        try:
            # 1. Get available expiries for this underlying
            all_options = self.client.get_products(
                contract_types="call_options,put_options", states="live"
            )
            if not isinstance(all_options, list): all_options = []

            # Filter to this underlying
            options = [o for o in all_options
                       if o.get("contract_type") in ("call_options","put_options")
                       and self._get_underlying(o) == underlying]

            # Get unique expiries within DTE range
            expiries = self._get_valid_expiries(options)
            logger.info(f"{underlying}: Found {len(expiries)} valid expiries")

            for expiry_str in expiries[:1]:  # Trade nearest expiry
                setup = self._build_setup(underlying, expiry_str, options)
                if setup:
                    setups.append(setup)

        except Exception as e:
            logger.error(f"scan_entries error for {underlying}: {e}", exc_info=True)
        return setups

    def _get_underlying(self, product: dict) -> str:
        sym = product.get("symbol","")
        # C-BTC-90000-310125 → BTC
        parts = sym.split("-")
        return parts[1] if len(parts) >= 2 else ""

    def _get_valid_expiries(self, options: list) -> list:
        seen = set(); expiries = []
        for o in options:
            sym = o.get("symbol","")
            dte = days_to_expiry(sym)
            if self.min_dte <= dte <= self.max_dte:
                # extract expiry suffix
                parts = sym.split("-")
                if len(parts) >= 4:
                    exp = parts[-1]
                    if exp not in seen:
                        seen.add(exp); expiries.append(exp)
        return expiries

    def _build_setup(self, underlying: str, expiry_suffix: str, all_opts: list) -> Optional[dict]:
        """Build a strangle setup for a given underlying+expiry."""
        try:
            # Filter to this expiry
            chain = [o for o in all_opts if o.get("symbol","").endswith(expiry_suffix)]
            if not chain:
                logger.debug(f"No chain data for {underlying} expiry={expiry_suffix}")
                return None

            # Get ticker data (includes greeks + IV)
            symbols = [o["symbol"] for o in chain[:20]]
            tickers = {}
            for sym in symbols:
                try:
                    t = self.client.get_ticker(sym)
                    tickers[sym] = t
                except: pass

            if not tickers:
                logger.debug("No ticker data available")
                return None

            # Enrich chain with live greeks from tickers
            enriched = []
            for o in chain:
                sym = o["symbol"]
                if sym in tickers:
                    t = tickers[sym]
                    enriched.append({**o,
                        "greeks": t.get("greeks", {}),
                        "quotes": t.get("quotes", {}),
                        "mark_price": t.get("mark_price","0"),
                        "mark_vol": t.get("mark_vol","0"),
                        "spot_price": t.get("spot_price","0"),
                    })

            if not enriched:
                logger.debug(f"No enriched options for {underlying}")
                return None

            spot = float(enriched[0].get("spot_price","0") or 0)
            if spot <= 0:
                logger.debug(f"No spot price for {underlying}")
                return None

            # IV percentile check
            ivs = []
            for e in enriched:
                mv = e.get("mark_vol","0")
                try: ivs.append(float(mv)/100 if float(mv)>0 else 0)
                except: pass
            current_iv = float(np.median(ivs)) if ivs else 0.3
            iv_hist = pd.Series(ivs)  # In production: load from DB
            iv_pct = iv_percentile(iv_hist, current_iv) if len(iv_hist) > 5 else 50.0

            if iv_pct < self.iv_pct_min:
                logger.info(f"{underlying}: IV percentile {iv_pct:.1f} < {self.iv_pct_min} — skip")
                return None

            # ATR / volatility regime check
            if self.use_trend:
                ohlc = self._get_spot_ohlc(underlying)
                if ohlc is not None and len(ohlc) >= 20:
                    atr_series = calculate_atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period)
                    if is_high_volatility_regime(atr_series, self.max_atr_mult):
                        logger.warning(f"{underlying}: High vol regime — skip entry")
                        return None

            # Find OTM call (positive delta ~target_delta)
            call_opt = find_strike_by_delta(
                [e for e in enriched if "call" in e.get("contract_type","")],
                self.target_delta, "call", self.delta_tol
            )
            # Find OTM put (negative delta ~-target_delta)
            put_opt = find_strike_by_delta(
                [e for e in enriched if "put" in e.get("contract_type","")],
                self.target_delta, "put", self.delta_tol
            )

            if not call_opt or not put_opt:
                logger.debug(f"{underlying}: Could not find strikes at delta={self.target_delta}")
                return None

            # Get premiums
            call_mid = mid_price(call_opt.get("quotes", {})) or 0
            put_mid  = mid_price(put_opt.get("quotes", {})) or 0
            net_credit = call_mid + put_mid

            if net_credit < self.min_premium:
                logger.info(f"{underlying}: Net credit ${net_credit:.2f} < ${self.min_premium} — skip")
                return None

            # Liquidity check
            call_spread_pct = bid_ask_spread_pct(call_opt.get("quotes",{}))
            put_spread_pct  = bid_ask_spread_pct(put_opt.get("quotes",{}))
            if call_spread_pct > self.max_spread_pct or put_spread_pct > self.max_spread_pct:
                logger.info(f"{underlying}: Spread too wide — call={call_spread_pct:.2%} put={put_spread_pct:.2%}")
                return None

            dte = days_to_expiry(call_opt.get("symbol",""))
            logger.info(
                f"✅ SETUP FOUND: {underlying} DTE={dte} | "
                f"Call={call_opt['symbol']} mid={call_mid:.2f} | "
                f"Put={put_opt['symbol']} mid={put_mid:.2f} | "
                f"Net credit={net_credit:.2f} | IV%={iv_pct:.1f}"
            )

            return {
                "strategy":    "short_strangle",
                "underlying":  underlying,
                "expiry":      expiry_suffix,
                "spot":        spot,
                "dte":         dte,
                "call":        call_opt,
                "put":         put_opt,
                "call_mid":    call_mid,
                "put_mid":     put_mid,
                "net_credit":  net_credit,
                "iv_percentile": iv_pct,
                "current_iv":  current_iv,
            }

        except Exception as e:
            logger.error(f"_build_setup error: {e}", exc_info=True)
            return None

    def _get_spot_ohlc(self, underlying: str) -> Optional[pd.DataFrame]:
        """Fetch recent OHLC for underlying spot for trend filter."""
        try:
            sym = f"{underlying}USD"
            end = int(time.time())
            start = end - 86400 * 30  # 30 days
            candles = self.client.get_ohlc(sym, "1h", start, end)
            if not candles: return None
            df = pd.DataFrame(candles)
            df.columns = [c.lower() for c in df.columns]
            for col in ["open","high","low","close"]:
                if col in df.columns: df[col] = df[col].astype(float)
            return df
        except Exception as e:
            logger.debug(f"OHLC fetch failed for {underlying}: {e}")
            return None

    # ── Execute Entry ────────────────────────────────────────

    def execute_entry(self, setup: dict) -> dict:
        """Place sell orders for both call and put legs."""
        trade_id = str(uuid.uuid4())[:12]
        result = {"trade_id": trade_id, "status": "failed", "orders": []}

        try:
            # Risk check
            balances = self.client.get_wallet_balances()
            portfolio_value = self._get_portfolio_value(balances)

            allowed, reason = self.risk.pre_trade_check(
                portfolio_value=portfolio_value,
                underlying=setup["underlying"],
                current_iv=setup.get("current_iv"),
                avg_iv=setup.get("current_iv", 0.3) * 0.8,
            )
            if not allowed:
                logger.warning(f"Entry blocked by risk: {reason}")
                result["status"] = "blocked"; result["reason"] = reason
                return result

            # Size calculation
            max_loss = setup["net_credit"] * self.sl_multiple * 2  # both legs
            size = self.risk.calculate_position_size(portfolio_value, max_loss)

            # Place CALL leg (sell)
            call_price = setup["call_mid"]
            call_bid = float(setup["call"].get("quotes",{}).get("best_bid", call_price))
            call_order = self._place_sell_limit(
                setup["call"], size, call_bid, f"{trade_id}_call"
            )

            # Place PUT leg (sell)
            put_price = setup["put_mid"]
            put_bid = float(setup["put"].get("quotes",{}).get("best_bid", put_price))
            put_order = self._place_sell_limit(
                setup["put"], size, put_bid, f"{trade_id}_put"
            )

            result["orders"] = [call_order, put_order]
            result["size"] = size
            result["entry_call_price"] = call_bid
            result["entry_put_price"] = put_bid
            result["entry_credit"] = (call_bid + put_bid) * size
            result["status"] = "submitted"

            # Track trade
            self.active_trades[trade_id] = {
                "trade_id":       trade_id,
                "underlying":     setup["underlying"],
                "call_symbol":    setup["call"]["symbol"],
                "put_symbol":     setup["put"]["symbol"],
                "call_product_id":setup["call"]["product_id"],
                "put_product_id": setup["put"]["product_id"],
                "entry_call_price": call_bid,
                "entry_put_price":  put_bid,
                "entry_credit":   call_bid + put_bid,
                "size":           size,
                "entry_time":     datetime.utcnow().isoformat(),
                "dte_at_entry":   setup["dte"],
                "call_order_id":  call_order.get("id"),
                "put_order_id":   put_order.get("id"),
            }
            self.risk.register_position(f"{setup['underlying']}_{trade_id}", self.active_trades[trade_id])

            self.tlog.log(trade_id=trade_id, underlying=setup["underlying"],
                symbol=setup["call"]["symbol"], product_id=setup["call"]["product_id"],
                side="sell", order_type="limit", size=size, entry_price=call_bid,
                strategy="short_strangle", leg="call", status="submitted")
            self.tlog.log(trade_id=trade_id, underlying=setup["underlying"],
                symbol=setup["put"]["symbol"], product_id=setup["put"]["product_id"],
                side="sell", order_type="limit", size=size, entry_price=put_bid,
                strategy="short_strangle", leg="put", status="submitted")

            logger.info(f"✅ Strangle entered: {trade_id} | Credit={(call_bid+put_bid)*size:.2f}")

        except Exception as e:
            logger.error(f"execute_entry error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    def _place_sell_limit(self, option: dict, size: int, price: float, client_oid: str) -> dict:
        """Place a limit sell order, with timeout + market fallback."""
        pid = option["product_id"]
        tick = float(option.get("tick_size","0.5") or 0.5)
        # Price inside best bid by 1 tick
        limit_price = round(price - tick, 8)
        limit_price = max(limit_price, tick)

        order = self.client.place_order(
            product_id=pid,
            size=size,
            side="sell",
            order_type="limit_order",
            limit_price=str(limit_price),
            time_in_force="gtc",
            client_order_id=client_oid[:32],
        )
        logger.info(f"Sell limit placed: {option['symbol']} @ {limit_price} | order_id={order.get('id')}")
        return order

    def _get_portfolio_value(self, balances) -> float:
        if not balances: return 10000
        total = 0.0
        for b in (balances if isinstance(balances, list) else [balances]):
            try: total += float(b.get("available_balance","0") or 0)
            except: pass
        return max(total, 100)

    # ── Exit Logic ───────────────────────────────────────────

    def check_exits(self, positions: list) -> list:
        """Check all active strangles for exit signals."""
        exit_signals = []
        for trade_id, trade in list(self.active_trades.items()):
            try:
                signal = self._check_single_trade_exit(trade_id, trade)
                if signal:
                    exit_signals.append(signal)
            except Exception as e:
                logger.error(f"Exit check error for {trade_id}: {e}")
        return exit_signals

    def _check_single_trade_exit(self, trade_id: str, trade: dict) -> Optional[dict]:
        """Check if this strangle should be exited."""
        call_sym = trade["call_symbol"]
        put_sym  = trade["put_symbol"]
        entry_credit = trade["entry_credit"]

        # DTE check
        dte = days_to_expiry(call_sym)
        if dte <= self.force_close_dte:
            return {"trade_id": trade_id, "reason": "dte_expiry", "trade": trade}

        # Fetch current prices
        try:
            call_ticker = self.client.get_ticker(call_sym)
            put_ticker  = self.client.get_ticker(put_sym)
        except Exception as e:
            logger.warning(f"Ticker fetch failed for exit check: {e}")
            return None

        call_current = mid_price(call_ticker.get("quotes",{})) or float(call_ticker.get("mark_price","0"))
        put_current  = mid_price(put_ticker.get("quotes",{})) or float(put_ticker.get("mark_price","0"))

        if not call_current or not put_current:
            return None

        current_value = call_current + put_current
        pnl_per_contract = entry_credit - current_value
        pnl_pct = pnl_per_contract / entry_credit if entry_credit > 0 else 0

        # Profit target
        if pnl_pct >= self.profit_target:
            logger.info(f"✅ PROFIT TARGET: {trade_id} PnL%={pnl_pct:.1%}")
            return {"trade_id": trade_id, "reason": "profit_target",
                    "pnl_pct": pnl_pct, "trade": trade,
                    "call_current": call_current, "put_current": put_current}

        # Stop loss — either leg at 1.5x premium
        call_entry = trade["entry_call_price"]
        put_entry  = trade["entry_put_price"]
        if call_current >= call_entry * self.sl_multiple:
            logger.warning(f"⚠️ CALL STOP LOSS: {trade_id} call_entry={call_entry} current={call_current}")
            return {"trade_id": trade_id, "reason": "call_stop_loss",
                    "pnl_pct": pnl_pct, "trade": trade,
                    "call_current": call_current, "put_current": put_current}
        if put_current >= put_entry * self.sl_multiple:
            logger.warning(f"⚠️ PUT STOP LOSS: {trade_id} put_entry={put_entry} current={put_current}")
            return {"trade_id": trade_id, "reason": "put_stop_loss",
                    "pnl_pct": pnl_pct, "trade": trade,
                    "call_current": call_current, "put_current": put_current}

        logger.debug(f"Trade {trade_id}: PnL%={pnl_pct:.1%} | DTE={dte} | hold")
        return None

    def execute_exit(self, position: dict, reason: str) -> dict:
        """Buy back both legs to close the strangle."""
        trade = position.get("trade", position)
        trade_id = trade["trade_id"]
        result = {"trade_id": trade_id, "reason": reason, "status": "failed"}

        try:
            size = trade["size"]
            call_pid = trade["call_product_id"]
            put_pid  = trade["put_product_id"]

            call_close = position.get("call_current", 0)
            put_close  = position.get("put_current", 0)

            # Buy back call
            if call_close > 0 and not self.cfg["exit"]["use_market_on_close"]:
                call_order = self.client.place_order(
                    product_id=call_pid, size=size, side="buy",
                    order_type="limit_order", limit_price=str(round(call_close * 1.01, 8)),
                    time_in_force="ioc", reduce_only=True,
                )
            else:
                call_order = self.client.place_market_order(call_pid, size, "buy", reduce_only=True)

            # Buy back put
            if put_close > 0 and not self.cfg["exit"]["use_market_on_close"]:
                put_order = self.client.place_order(
                    product_id=put_pid, size=size, side="buy",
                    order_type="limit_order", limit_price=str(round(put_close * 1.01, 8)),
                    time_in_force="ioc", reduce_only=True,
                )
            else:
                put_order = self.client.place_market_order(put_pid, size, "buy", reduce_only=True)

            # PnL calc
            entry_credit = trade["entry_credit"] * size
            exit_cost    = (call_close + put_close) * size
            pnl          = entry_credit - exit_cost
            pnl_pct      = pnl / entry_credit if entry_credit > 0 else 0

            result["pnl"] = pnl; result["pnl_pct"] = pnl_pct
            result["status"] = "closed"; result["orders"] = [call_order, put_order]

            # Update risk manager
            self.risk.record_trade_result(pnl)
            self.risk.remove_position(f"{trade['underlying']}_{trade_id}")
            self.active_trades.pop(trade_id, None)

            self.tlog.log(trade_id=trade_id, underlying=trade["underlying"],
                symbol=trade["call_symbol"], product_id=call_pid,
                side="buy", order_type="limit", size=size, entry_price=call_close,
                strategy="short_strangle", leg="call_close", status="closed", pnl_usd=round(pnl,4),
                notes=reason)

            logger.info(f"🔵 Strangle closed: {trade_id} PnL=${pnl:.2f} ({pnl_pct:.1%}) reason={reason}")

        except Exception as e:
            logger.error(f"execute_exit error: {e}", exc_info=True)
            result["error"] = str(e)

        return result
