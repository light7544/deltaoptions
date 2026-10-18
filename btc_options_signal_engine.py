import os
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/options-signals", tags=["BTC Options Signal Engine"])

DELTA_BASE_URL = "https://api.india.delta.exchange"
UNDERLYING = "BTC"

# ==========================================
# DATA MODELS
# ==========================================

class OptionStrikeData(BaseModel):
    strike: float
    option_type: str = Field(..., description="'call' or 'put'")
    open_interest: float
    volume_24h: float = 0.0
    oi_change_24h: float = 0.0
    iv: float = Field(..., description="Implied Volatility as decimal (e.g., 0.55 for 55%)")
    delta: float
    gamma: float
    theta: Optional[float] = 0.0
    vega: Optional[float] = 0.0
    mark_price: float = 0.0

class OptionChainPayload(BaseModel):
    symbol: str = "BTCUSDT"
    spot_price: float
    hours_to_expiry: float
    historical_pcr_mean: Optional[float] = 0.95
    chain: List[OptionStrikeData]

class SignalOutput(BaseModel):
    symbol: str
    spot_price: float
    composite_score: float  # -100 (Strong Sell) to +100 (Strong Buy)
    primary_signal: str     # STRONG_BUY, BUY, NEUTRAL_RANGE, SELL, STRONG_SELL
    recommended_strategy: str
    regime: str             # POSITIVE_GAMMA (Mean Reverting) vs NEGATIVE_GAMMA (Trending/Breakout)
    key_levels: Dict[str, float]
    metrics_breakdown: Dict[str, Any]
    component_scores: Dict[str, float]
    expiry: Optional[str] = None
    available_expiries: Optional[List[str]] = []
    strikes_chart_data: Optional[List[Dict[str, Any]]] = []

# ==========================================
# CORE QUANTITATIVE ENGINE
# ==========================================

class BTCOptionsQuantEngine:
    def __init__(self, payload: OptionChainPayload):
        self.spot = payload.spot_price
        self.hours_to_expiry = max(payload.hours_to_expiry, 0.5)
        self.hist_pcr = payload.historical_pcr_mean or 0.95
        self.df = pd.DataFrame([item.model_dump() for item in payload.chain])
        if not self.df.empty and 'option_type' in self.df.columns:
            self.df['option_type'] = self.df['option_type'].str.lower()

    def calculate_oi_walls_and_flow(self) -> Dict[str, Any]:
        """1. Strike Concentration, OI Walls & Intraday Delta OI Flow (Weight: 50% combined)"""
        calls = self.df[self.df['option_type'] == 'call']
        puts = self.df[self.df['option_type'] == 'put']

        call_wall = calls.loc[calls['open_interest'].idxmax()]['strike'] if not calls.empty and calls['open_interest'].max() > 0 else self.spot * 1.05
        put_wall = puts.loc[puts['open_interest'].idxmax()]['strike'] if not puts.empty and puts['open_interest'].max() > 0 else self.spot * 0.95

        # Calculate position within the Put-Call channel (0 = at Put Wall, 1 = at Call Wall)
        channel_width = max(call_wall - put_wall, 1.0)
        relative_pos = (self.spot - put_wall) / channel_width

        # Wall Score: Near Put Wall (Support) = Bullish (+), Near Call Wall (Resistance) = Bearish (-)
        # If price breaks above Call Wall with high Call OI change -> Gamma Squeeze Breakout (+)
        if self.spot > call_wall:
            wall_score = 75.0  # Gamma squeeze breakout above resistance
        elif self.spot < put_wall:
            wall_score = -75.0 # Breakdown below support wall
        else:
            # Linear interpolation: closer to put_wall -> +80, closer to call_wall -> -80
            wall_score = (0.5 - relative_pos) * 160.0
            wall_score = float(np.clip(wall_score, -100.0, 100.0))

        # Intraday OI Flow Score (Put writing vs Call writing near spot)
        atm_band = self.df[(self.df['strike'] >= self.spot * 0.94) & (self.df['strike'] <= self.spot * 1.06)]
        net_put_oi_change = atm_band[atm_band['option_type'] == 'put']['oi_change_24h'].sum() if not atm_band.empty else 0.0
        net_call_oi_change = atm_band[atm_band['option_type'] == 'call']['oi_change_24h'].sum() if not atm_band.empty else 0.0

        total_flow = abs(net_put_oi_change) + abs(net_call_oi_change)
        if total_flow > 0:
            # Rising Put OI near/below spot = Aggressive Put Writing (Bullish support building)
            # Rising Call OI near/above spot = Aggressive Call Writing (Bearish overhead supply)
            flow_score = ((net_put_oi_change - net_call_oi_change) / total_flow) * 100.0
        else:
            flow_score = 0.0

        return {
            "put_wall_support": float(put_wall),
            "call_wall_resistance": float(call_wall),
            "wall_score": round(float(wall_score), 2),
            "flow_score": round(float(np.clip(flow_score, -100, 100)), 2),
            "net_put_oi_change": float(net_put_oi_change),
            "net_call_oi_change": float(net_call_oi_change)
        }

    def calculate_pcr_signal(self) -> Dict[str, Any]:
        """2. Put-Call Ratio (OI & Volume) Dynamics (Weight: 20%)"""
        calls = self.df[self.df['option_type'] == 'call']
        puts = self.df[self.df['option_type'] == 'put']

        total_call_oi = calls['open_interest'].sum() if not calls.empty else 1.0
        total_put_oi = puts['open_interest'].sum() if not puts.empty else 0.0
        pcr_oi = total_put_oi / max(total_call_oi, 1.0)

        total_call_vol = calls['volume_24h'].sum() if not calls.empty else 1.0
        total_put_vol = puts['volume_24h'].sum() if not puts.empty else 0.0
        pcr_vol = total_put_vol / max(total_call_vol, 1.0)

        # Contrarian & Put-Selling interpretation:
        # High PCR (>1.35) = Over-hedged / Heavy Put support -> Bullish (+80)
        # Extremely Low PCR (<0.60) = Euphoric Call buying -> Bearish (-80)
        if pcr_oi >= 1.35:
            pcr_score = min(100.0, 50.0 + (pcr_oi - 1.35) * 100.0)
        elif pcr_oi <= 0.60:
            pcr_score = max(-100.0, -50.0 - (0.60 - pcr_oi) * 150.0)
        else:
            # Deviation from neutral 0.95
            pcr_score = ((pcr_oi - self.hist_pcr) / 0.40) * 50.0

        return {
            "pcr_oi": round(float(pcr_oi), 3),
            "pcr_volume": round(float(pcr_vol), 3),
            "pcr_score": round(float(np.clip(pcr_score, -100, 100)), 2)
        }

    def calculate_max_pain(self) -> Dict[str, Any]:
        """3. Exact Vectorized Max Pain Convergence (Weight: 15%)"""
        strikes = np.sort(self.df['strike'].unique())
        if len(strikes) == 0:
            return {
                "max_pain_strike": self.spot,
                "distance_pct": 0.0,
                "max_pain_score": 0.0
            }

        calls = self.df[self.df['option_type'] == 'call']
        puts = self.df[self.df['option_type'] == 'put']

        pain_values = []
        for target_price in strikes:
            # Intrinsic value paid out to option buyers if BTC expires at target_price
            call_loss = (np.maximum(0.0, target_price - calls['strike']) * calls['open_interest']).sum() if not calls.empty else 0.0
            put_loss = (np.maximum(0.0, puts['strike'] - target_price) * puts['open_interest']).sum() if not puts.empty else 0.0
            pain_values.append(call_loss + put_loss)

        min_idx = int(np.argmin(pain_values))
        max_pain_strike = float(strikes[min_idx])

        # Distance from Spot to Max Pain
        pct_diff = (max_pain_strike - self.spot) / self.spot  # Positive if Max Pain is above Spot

        # Decay factor: Max Pain gravitational pull is strongest when hours_to_expiry <= 24h
        expiry_multiplier = 1.0 if self.hours_to_expiry <= 24 else max(0.25, 24.0 / self.hours_to_expiry)
        raw_mp_score = (pct_diff / 0.04) * 100.0 * expiry_multiplier  # 4% distance = full score

        return {
            "max_pain_strike": max_pain_strike,
            "distance_pct": round(float(pct_diff * 100), 2),
            "max_pain_score": round(float(np.clip(raw_mp_score, -100, 100)), 2)
        }

    def calculate_25d_skew(self) -> Dict[str, Any]:
        """4. 25-Delta Implied Volatility Skew (Weight: 15%)"""
        calls = self.df[self.df['option_type'] == 'call'].copy()
        puts = self.df[self.df['option_type'] == 'put'].copy()

        if calls.empty or puts.empty:
            return {"iv_25d_put": 0.0, "iv_25d_call": 0.0, "skew_25d": 0.0, "skew_score": 0.0}

        # Find strike closest to +0.25 delta for Calls and -0.25 delta for Puts
        calls['delta_diff'] = (calls['delta'].abs() - 0.25).abs()
        puts['delta_diff'] = (puts['delta'].abs() - 0.25).abs()

        iv_25d_call = float(calls.loc[calls['delta_diff'].idxmin()]['iv'])
        iv_25d_put = float(puts.loc[puts['delta_diff'].idxmin()]['iv'])

        # Skew = Put IV - Call IV (in percentage points, e.g., 0.05 = +5% Put skew)
        skew = (iv_25d_put - iv_25d_call) * 100.0

        # Negative skew (Calls more expensive than Puts) = Bullish momentum (+ score)
        # Moderate Positive skew (0 to +5%) = Fear / Downside hedging (- score)
        # Extreme Positive skew (> +12%) = Capitulation / Contrarian Buy reversal
        if skew > 12.0:
            skew_score = 65.0  # Extreme panic exhaustion -> Contrarian Buy
        else:
            skew_score = float(np.clip(-skew * 10.0, -100.0, 100.0))

        return {
            "iv_25d_put": round(iv_25d_put * 100, 2),
            "iv_25d_call": round(iv_25d_call * 100, 2),
            "skew_25d_pct": round(skew, 2),
            "skew_score": round(skew_score, 2)
        }

    def calculate_net_gex(self) -> Dict[str, Any]:
        """5. Net Gamma Exposure (GEX) & Zero-Gamma Flip Level"""
        df_gex = self.df.copy()
        if df_gex.empty:
            return {
                "net_gex_usd_m": 0.0,
                "zero_gamma_flip_level": self.spot,
                "regime": "POSITIVE_GAMMA_MEAN_REVERTING"
            }

        # Standard Assumption: Dealers are net short puts & long calls (or vice-versa).
        # Call Gamma contributes positively to dealer GEX, Put Gamma contributes negatively.
        df_gex['signed_gamma'] = np.where(
            df_gex['option_type'] == 'call',
            df_gex['gamma'] * df_gex['open_interest'] * (self.spot ** 2) * 0.01,
            -1.0 * df_gex['gamma'] * df_gex['open_interest'] * (self.spot ** 2) * 0.01
        )

        net_gex = float(df_gex['signed_gamma'].sum())
        strike_gex = df_gex.groupby('strike')['signed_gamma'].sum().reset_index()

        # Find Zero-Gamma Flip Level (where cumulative GEX flips sign near spot)
        strike_gex['cum_gex'] = strike_gex['signed_gamma'].cumsum()
        zero_gamma_strike = float(
            strike_gex.iloc[(strike_gex['cum_gex'].abs()).argsort()[:1]]['strike'].values[0]
        ) if not strike_gex.empty else self.spot

        regime = "POSITIVE_GAMMA_MEAN_REVERTING" if net_gex >= 0 else "NEGATIVE_GAMMA_VOLATILE_TREND"

        return {
            "net_gex_usd_m": round(net_gex / 1e6, 3),
            "zero_gamma_flip_level": zero_gamma_strike,
            "regime": regime
        }

    def generate_master_signal(self) -> SignalOutput:
        oi_data = self.calculate_oi_walls_and_flow()
        pcr_data = self.calculate_pcr_signal()
        mp_data = self.calculate_max_pain()
        skew_data = self.calculate_25d_skew()
        gex_data = self.calculate_net_gex()

        # Weighted Composite Score (-100 to +100)
        composite = (
            (oi_data['wall_score'] * 0.25) +
            (oi_data['flow_score'] * 0.25) +
            (pcr_data['pcr_score'] * 0.20) +
            (skew_data['skew_score'] * 0.15) +
            (mp_data['max_pain_score'] * 0.15)
        )

        # Amplify signal by 15% if in Negative Gamma (Breakout / Momentum regime)
        if gex_data['regime'] == "NEGATIVE_GAMMA_VOLATILE_TREND":
            composite = float(np.clip(composite * 1.15, -100.0, 100.0))
        else:
            composite = float(np.clip(composite, -100.0, 100.0))

        # Classification & Actionable Strategy Mapping
        if composite >= 55:
            signal = "STRONG_BUY"
            strategy = (
                f"Directional Long BTC / Sell OTM Put Credit Spreads at {oi_data['put_wall_support']} Support"
                if gex_data['regime'] == "POSITIVE_GAMMA_MEAN_REVERTING"
                else f"Momentum Long BTC Futures / Buy OTM Calls targeting {oi_data['call_wall_resistance']}"
            )
        elif composite >= 20:
            signal = "BUY"
            strategy = f"Bullish Bias: Sell OTM Puts at {oi_data['put_wall_support']} (Theta Harvest) or Accumulate Spot Dips"
        elif composite <= -55:
            signal = "STRONG_SELL"
            strategy = (
                f"Directional Short BTC / Sell OTM Call Credit Spreads at {oi_data['call_wall_resistance']}"
                if gex_data['regime'] == "POSITIVE_GAMMA_MEAN_REVERTING"
                else f"Momentum Short BTC Futures / Buy Put Debit Spreads below {gex_data['zero_gamma_flip_level']}"
            )
        elif composite <= -20:
            signal = "SELL"
            strategy = f"Bearish Bias: Sell OTM Calls at {oi_data['call_wall_resistance']} or Hedge Longs"
        else:
            signal = "NEUTRAL_RANGE"
            strategy = (
                f"High-Probability Theta Decay: Sell Short Strangle / Iron Condor between "
                f"{oi_data['put_wall_support']} (Put Strike) and {oi_data['call_wall_resistance']} (Call Strike)"
            )

        # Build Strike Chart Data (Call vs Put OI & GEX per strike)
        strikes_chart = []
        if not self.df.empty:
            grouped = self.df.groupby('strike')
            for st, grp in grouped:
                call_row = grp[grp['option_type'] == 'call']
                put_row = grp[grp['option_type'] == 'put']
                c_oi = float(call_row['open_interest'].sum()) if not call_row.empty else 0.0
                p_oi = float(put_row['open_interest'].sum()) if not put_row.empty else 0.0
                c_gamma = float(call_row['gamma'].sum()) if not call_row.empty else 0.0
                p_gamma = float(put_row['gamma'].sum()) if not put_row.empty else 0.0
                c_iv = float(call_row['iv'].mean()) if not call_row.empty else 0.0
                p_iv = float(put_row['iv'].mean()) if not put_row.empty else 0.0

                strikes_chart.append({
                    "strike": float(st),
                    "call_oi": c_oi,
                    "put_oi": p_oi,
                    "call_gamma": c_gamma,
                    "put_gamma": p_gamma,
                    "call_iv": c_iv,
                    "put_iv": p_iv,
                    "total_oi": c_oi + p_oi
                })
            strikes_chart.sort(key=lambda x: x["strike"])

        return SignalOutput(
            symbol="BTCUSDT",
            spot_price=self.spot,
            composite_score=round(composite, 2),
            primary_signal=signal,
            recommended_strategy=strategy,
            regime=gex_data['regime'],
            key_levels={
                "support_put_wall": oi_data['put_wall_support'],
                "resistance_call_wall": oi_data['call_wall_resistance'],
                "max_pain_magnet": mp_data['max_pain_strike'],
                "zero_gamma_flip": gex_data['zero_gamma_flip_level']
            },
            metrics_breakdown={
                "oi_metrics": oi_data,
                "pcr_metrics": pcr_data,
                "max_pain_metrics": mp_data,
                "skew_metrics": skew_data,
                "gex_metrics": gex_data
            },
            component_scores={
                "oi_wall_positioning (25%)": oi_data['wall_score'],
                "intraday_oi_flow (25%)": oi_data['flow_score'],
                "pcr_sentiment (20%)": pcr_data['pcr_score'],
                "iv_25d_skew (15%)": skew_data['skew_score'],
                "max_pain_gravity (15%)": mp_data['max_pain_score']
            },
            strikes_chart_data=strikes_chart
        )

# ==========================================
# ADAPTER SERVICE & FASTAPI ENDPOINTS
# ==========================================

def fetch_live_delta_chain_payload(target_expiry: Optional[str] = None) -> tuple[OptionChainPayload, str, List[str]]:
    """
    Adapter service: Pulls live products & tickers from Delta Exchange,
    parses Greeks, IV, OI, and compiles into OptionChainPayload.
    """
    url_p = f"{DELTA_BASE_URL}/v2/products"
    params_p = {"contract_types": "call_options,put_options", "states": "live"}
    resp_p = requests.get(url_p, params=params_p, timeout=12)
    resp_p.raise_for_status()
    all_products = resp_p.json().get("result", [])

    btc_products = []
    for p in all_products:
        sym = (
            p.get("underlying_asset", {}).get("symbol")
            if isinstance(p.get("underlying_asset"), dict)
            else p.get("underlying_asset")
        )
        if sym == UNDERLYING and p.get("strike_price"):
            btc_products.append(p)

    if not btc_products:
        raise HTTPException(status_code=502, detail="No BTC option products returned by exchange.")

    # Collect unique expiries
    expiries_map = {}
    for p in btc_products:
        settlement = p.get("settlement_time")
        if settlement:
            exp_date = settlement[:10]
            expiries_map.setdefault(exp_date, settlement)

    available_expiries = sorted(list(expiries_map.keys()))
    if not available_expiries:
        raise HTTPException(status_code=502, detail="No active BTC option expiries found.")

    chosen_expiry = target_expiry if target_expiry in available_expiries else available_expiries[0]
    settlement_iso = expiries_map[chosen_expiry]

    # Calculate hours to expiry
    try:
        settlement_dt = datetime.fromisoformat(settlement_iso.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        diff_hours = (settlement_dt - now_dt).total_seconds() / 3600.0
        hours_to_expiry = max(diff_hours, 1.0)
    except Exception:
        hours_to_expiry = 24.0

    # Fetch live tickers
    url_t = f"{DELTA_BASE_URL}/v2/tickers"
    params_t = {"contract_types": "call_options,put_options"}
    resp_t = requests.get(url_t, params=params_t, timeout=12)
    resp_t.raise_for_status()
    raw_tickers = resp_t.json().get("result", [])
    tickers = {t.get("symbol"): t for t in raw_tickers if t.get("symbol")}

    spot_price = 0.0
    chain_items = []

    for p in btc_products:
        if p.get("settlement_time", "")[:10] != chosen_expiry:
            continue
        sym = p.get("symbol")
        if sym not in tickers:
            continue
        t = tickers[sym]

        if not spot_price and t.get("spot_price"):
            try:
                spot_price = float(t.get("spot_price"))
            except (ValueError, TypeError):
                pass

        contract_type = p.get("contract_type", "")
        side = "call" if "call" in contract_type else "put"
        strike = float(p.get("strike_price") or 0.0)

        greeks = t.get("greeks") or {}
        try:
            delta = float(greeks.get("delta") or (0.5 if side == "call" else -0.5))
        except (ValueError, TypeError):
            delta = 0.5 if side == "call" else -0.5

        try:
            gamma = float(greeks.get("gamma") or 0.00005)
        except (ValueError, TypeError):
            gamma = 0.00005

        try:
            theta = float(greeks.get("theta") or 0.0)
            vega = float(greeks.get("vega") or 0.0)
        except (ValueError, TypeError):
            theta, vega = 0.0, 0.0

        try:
            mark_vol = float(t.get("mark_vol") or (t.get("quotes", {}).get("mark_iv") if t.get("quotes") else None) or 0.55)
        except (ValueError, TypeError):
            mark_vol = 0.55

        try:
            oi = float(t.get("oi") or 0.0)
        except (ValueError, TypeError):
            oi = 0.0

        try:
            volume_24h = float(t.get("volume") or 0.0)
        except (ValueError, TypeError):
            volume_24h = 0.0

        try:
            mark_price = float(t.get("mark_price") or 0.0)
        except (ValueError, TypeError):
            mark_price = 0.0

        # Check for 24h OI change in ticker or calculate from ticker fields
        try:
            oi_change = float(t.get("oi_change_usd_6h") or t.get("oi_change_24h") or 0.0)
        except (ValueError, TypeError):
            oi_change = 0.0

        chain_items.append(
            OptionStrikeData(
                strike=strike,
                option_type=side,
                open_interest=oi,
                volume_24h=volume_24h,
                oi_change_24h=oi_change,
                iv=mark_vol,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                mark_price=mark_price
            )
        )

    if not spot_price:
        spot_price = 82000.0

    payload = OptionChainPayload(
        symbol="BTCUSDT",
        spot_price=spot_price,
        hours_to_expiry=hours_to_expiry,
        historical_pcr_mean=0.95,
        chain=chain_items
    )
    return payload, chosen_expiry, available_expiries


@router.post("/analyze", response_model=SignalOutput)
async def analyze_btc_option_chain(payload: OptionChainPayload):
    if not payload.chain:
        raise HTTPException(status_code=400, detail="Option chain array cannot be empty.")
    engine = BTCOptionsQuantEngine(payload)
    return engine.generate_master_signal()


@router.get("/expiries", response_model=List[str])
async def get_live_expiries():
    try:
        _, _, expiries = fetch_live_delta_chain_payload()
        return expiries
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live", response_model=SignalOutput)
async def get_live_signal(expiry: Optional[str] = None):
    try:
        payload, chosen_expiry, expiries = fetch_live_delta_chain_payload(expiry)
        engine = BTCOptionsQuantEngine(payload)
        signal = engine.generate_master_signal()
        signal.expiry = chosen_expiry
        signal.available_expiries = expiries
        return signal
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Live signal generation failed: {str(e)}")


@router.get("/live/{expiry}", response_model=SignalOutput)
async def get_live_signal_for_expiry(expiry: str):
    return await get_live_signal(expiry=expiry)
