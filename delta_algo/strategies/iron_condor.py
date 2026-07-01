"""
Iron Condor Strategy
====================
Sell OTM call spread + sell OTM put spread.
Defined risk — max loss is the width of the wings minus credit.
"""
import logging, uuid
from datetime import datetime
from typing import Optional
from strategies.base_strategy import BaseStrategy
from utils.options_math import (find_strike_by_delta, days_to_expiry,
    mid_price, bid_ask_spread_pct, iv_percentile)
import numpy as np

logger = logging.getLogger(__name__)


class IronCondorStrategy(BaseStrategy):
    def __init__(self, client, cfg, risk_manager, trade_logger):
        super().__init__(client, cfg, risk_manager, trade_logger)
        s = cfg["strategy"]
        self.target_delta  = s["target_delta"]
        self.delta_tol     = s["delta_tolerance"]
        self.wing_width    = s.get("ic_wing_width_pct", 0.05)
        self.iv_pct_min    = s["iv_percentile_min"]
        self.min_dte       = cfg["instruments"]["min_dte"]
        self.max_dte       = cfg["instruments"]["max_dte"]
        self.min_premium   = cfg["entry"]["min_premium_usd"]
        self.profit_target = cfg["exit"]["profit_target_pct"]
        self.sl_multiple   = cfg["exit"]["stop_loss_multiple"]
        self.force_close_dte = cfg["exit"]["max_dte_to_force_close"]
        self.active_trades: dict = {}

    def scan_entries(self, underlying: str) -> list:
        """Find iron condor setups."""
        # Reuse strangle scan then find protective wings
        setups = []
        try:
            all_options = self.client.get_products(
                contract_types="call_options,put_options", states="live"
            )
            if not isinstance(all_options, list): return []
            options = [o for o in all_options if self._get_underlying(o) == underlying]
            expiries = self._get_valid_expiries(options)
            for exp in expiries[:1]:
                setup = self._build_ic_setup(underlying, exp, options)
                if setup: setups.append(setup)
        except Exception as e:
            logger.error(f"IC scan error: {e}", exc_info=True)
        return setups

    def _get_underlying(self, p):
        parts = p.get("symbol","").split("-")
        return parts[1] if len(parts)>=2 else ""

    def _get_valid_expiries(self, options):
        seen=set(); result=[]
        for o in options:
            dte = days_to_expiry(o.get("symbol",""))
            if self.min_dte <= dte <= self.max_dte:
                exp = o.get("symbol","").split("-")[-1]
                if exp not in seen: seen.add(exp); result.append(exp)
        return result

    def _build_ic_setup(self, underlying, expiry, all_opts):
        try:
            chain = [o for o in all_opts if o.get("symbol","").endswith(expiry)]
            if not chain: return None

            # Enrich with tickers
            enriched = []
            for o in chain[:20]:
                try:
                    t = self.client.get_ticker(o["symbol"])
                    enriched.append({**o,"greeks":t.get("greeks",{}),"quotes":t.get("quotes",{}),
                                     "mark_price":t.get("mark_price","0"),"spot_price":t.get("spot_price","0")})
                except: pass

            if not enriched: return None
            spot = float(enriched[0].get("spot_price","0") or 0)
            if spot <= 0: return None

            # Short strikes at target_delta
            short_call = find_strike_by_delta([e for e in enriched if "call" in e.get("contract_type","")], self.target_delta, "call", self.delta_tol)
            short_put  = find_strike_by_delta([e for e in enriched if "put" in e.get("contract_type","")], self.target_delta, "put", self.delta_tol)
            if not short_call or not short_put: return None

            # Long strikes (wing protection) further OTM
            long_call = find_strike_by_delta([e for e in enriched if "call" in e.get("contract_type","")], self.target_delta * 0.5, "call", self.delta_tol)
            long_put  = find_strike_by_delta([e for e in enriched if "put" in e.get("contract_type","")], self.target_delta * 0.5, "put", self.delta_tol)
            if not long_call or not long_put: return None

            sc_mid = mid_price(short_call.get("quotes",{})) or 0
            sp_mid = mid_price(short_put.get("quotes",{})) or 0
            lc_mid = mid_price(long_call.get("quotes",{})) or 0
            lp_mid = mid_price(long_put.get("quotes",{})) or 0
            net_credit = (sc_mid + sp_mid) - (lc_mid + lp_mid)

            if net_credit < self.min_premium: return None

            dte = days_to_expiry(short_call.get("symbol",""))
            logger.info(f"✅ IC SETUP: {underlying} DTE={dte} credit={net_credit:.2f}")

            return {"strategy":"iron_condor","underlying":underlying,"expiry":expiry,
                    "spot":spot,"dte":dte,"short_call":short_call,"short_put":short_put,
                    "long_call":long_call,"long_put":long_put,
                    "sc_mid":sc_mid,"sp_mid":sp_mid,"lc_mid":lc_mid,"lp_mid":lp_mid,
                    "net_credit":net_credit}
        except Exception as e:
            logger.error(f"IC build setup error: {e}"); return None

    def execute_entry(self, setup: dict) -> dict:
        trade_id = str(uuid.uuid4())[:12]
        result = {"trade_id":trade_id,"status":"failed"}
        try:
            balances = self.client.get_wallet_balances()
            pv = sum(float(b.get("available_balance","0") or 0) for b in (balances if isinstance(balances,list) else []))
            pv = max(pv, 100)

            allowed, reason = self.risk.pre_trade_check(pv, setup["underlying"])
            if not allowed: return {"trade_id":trade_id,"status":"blocked","reason":reason}

            size = self.risk.calculate_position_size(pv, setup["net_credit"] * 2)

            # Batch place all 4 legs
            product_id = setup["short_call"]["product_id"]
            orders_data = [
                {"size":size,"side":"sell","order_type":"limit_order","limit_price":str(round(setup["sc_mid"]*0.99,8)),"client_order_id":f"{trade_id}_sc"},
                {"size":size,"side":"sell","order_type":"limit_order","limit_price":str(round(setup["sp_mid"]*0.99,8)),"client_order_id":f"{trade_id}_sp"},
                {"size":size,"side":"buy", "order_type":"limit_order","limit_price":str(round(setup["lc_mid"]*1.01,8)),"client_order_id":f"{trade_id}_lc"},
                {"size":size,"side":"buy", "order_type":"limit_order","limit_price":str(round(setup["lp_mid"]*1.01,8)),"client_order_id":f"{trade_id}_lp"},
            ]

            # Place each order separately (batch requires same product_id)
            placed = []
            for i, o_data in enumerate(orders_data):
                leg_map = [setup["short_call"],setup["short_put"],setup["long_call"],setup["long_put"]]
                pid = leg_map[i]["product_id"]
                o = self.client.place_order(pid, o_data["size"], o_data["side"],
                    o_data["order_type"], limit_price=o_data["limit_price"],
                    time_in_force="gtc", client_order_id=o_data["client_order_id"])
                placed.append(o)

            self.active_trades[trade_id] = {**setup,"trade_id":trade_id,"size":size,"orders":placed,"entry_time":datetime.utcnow().isoformat()}
            self.risk.register_position(f"{setup['underlying']}_{trade_id}", self.active_trades[trade_id])
            result.update({"status":"submitted","orders":placed,"trade_id":trade_id})
            logger.info(f"✅ IC entered: {trade_id} credit={setup['net_credit']*size:.2f}")
        except Exception as e:
            logger.error(f"IC execute_entry: {e}"); result["error"]=str(e)
        return result

    def check_exits(self, positions) -> list:
        signals = []
        for tid, trade in list(self.active_trades.items()):
            try:
                dte = days_to_expiry(trade["short_call"].get("symbol",""))
                if dte <= self.force_close_dte:
                    signals.append({"trade_id":tid,"reason":"dte_expiry","trade":trade})
                    continue
                sc_t = self.client.get_ticker(trade["short_call"]["symbol"])
                sp_t = self.client.get_ticker(trade["short_put"]["symbol"])
                sc_curr = mid_price(sc_t.get("quotes",{})) or float(sc_t.get("mark_price","0"))
                sp_curr = mid_price(sp_t.get("quotes",{})) or float(sp_t.get("mark_price","0"))
                current_cost = (sc_curr + sp_curr)  # simplified (ignoring longs)
                entry_credit = trade["sc_mid"] + trade["sp_mid"]
                pnl_pct = (entry_credit - current_cost) / entry_credit if entry_credit > 0 else 0
                if pnl_pct >= self.profit_target:
                    signals.append({"trade_id":tid,"reason":"profit_target","trade":trade,"pnl_pct":pnl_pct})
                elif sc_curr >= trade["sc_mid"]*self.sl_multiple:
                    signals.append({"trade_id":tid,"reason":"call_stop","trade":trade})
                elif sp_curr >= trade["sp_mid"]*self.sl_multiple:
                    signals.append({"trade_id":tid,"reason":"put_stop","trade":trade})
            except Exception as e:
                logger.error(f"IC exit check: {e}")
        return signals

    def execute_exit(self, position: dict, reason: str) -> dict:
        trade = position.get("trade", position)
        tid = trade["trade_id"]
        try:
            size = trade["size"]
            for leg, side in [("short_call","buy"),("short_put","buy"),("long_call","sell"),("long_put","sell")]:
                if leg in trade:
                    self.client.place_market_order(trade[leg]["product_id"], size, side, reduce_only=True)
            self.risk.record_trade_result(position.get("pnl_pct",0)*trade.get("net_credit",100)*size)
            self.risk.remove_position(f"{trade['underlying']}_{tid}")
            self.active_trades.pop(tid, None)
            logger.info(f"IC closed: {tid} reason={reason}")
            return {"status":"closed","trade_id":tid}
        except Exception as e:
            logger.error(f"IC execute_exit: {e}")
            return {"status":"failed","error":str(e)}
