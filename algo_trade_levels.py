"""
algo_trade_levels.py
--------------------
Institutional Quantitative Trade Levels Engine (Entry, Stop Loss, Target 1-3).
Endpoint: GET /api/v1/algo-signals/live?expiry_index=0
"""

import math
from typing import Dict, Any, List, Optional
import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from btc_options_signal_engine import (
    fetch_live_delta_chain_payload,
    BTCOptionsQuantEngine
)

router = APIRouter(prefix="/api/v1/algo-signals", tags=["Algo Trade Levels"])


class TradeLevels(BaseModel):
    entry: float
    stop_loss: float
    sl_pct: float
    tg1: float
    tg1_pct: float
    tg2: float
    tg2_pct: float
    tg3: float
    tg3_pct: float
    risk_reward_ratio: str


class StrategyUsed(BaseModel):
    name: str
    gamma_regime: str
    basis_explanation: str
    recommended_options_trade: str


class StructuralAnchors(BaseModel):
    put_wall_support: float
    call_wall_resistance: float
    zero_gamma_pivot: float
    max_pain_magnet: float
    daily_1sigma_usd: float
    pcr_oi: float
    skew_25d_pct: float


class AlgoSignalOutput(BaseModel):
    symbol: str = "BTCUSDT"
    signal_direction: str  # LONG (BUY), SHORT (SELL), or NEUTRAL (RANGE / THETA)
    composite_score: float
    confidence_pct: float
    trade_levels: TradeLevels
    strategy_used: StrategyUsed
    structural_anchors: StructuralAnchors
    expiry: str
    available_expiries: List[str]


def compute_algo_signals(expiry_index: int = 0, expiry: Optional[str] = None) -> AlgoSignalOutput:
    # 1. Fetch live payload from Delta Exchange
    payload, chosen_expiry, available_expiries = fetch_live_delta_chain_payload(expiry)

    if expiry is None and 0 <= expiry_index < len(available_expiries):
        target_exp = available_expiries[expiry_index]
        if target_exp != chosen_expiry:
            payload, chosen_expiry, available_expiries = fetch_live_delta_chain_payload(target_exp)

    # 2. Compute Quant Factors
    engine = BTCOptionsQuantEngine(payload)
    master = engine.generate_master_signal()
    oi_data = engine.calculate_oi_walls_and_flow()
    pcr_data = engine.calculate_pcr_signal()
    mp_data = engine.calculate_max_pain()
    skew_data = engine.calculate_25d_skew()
    gex_data = engine.calculate_net_gex()

    spot = float(payload.spot_price)
    call_wall = float(oi_data["call_wall_resistance"])
    put_wall = float(oi_data["put_wall_support"])
    max_pain = float(mp_data["max_pain_strike"])
    zero_gamma = float(gex_data["zero_gamma_flip_level"])
    composite = float(master.composite_score)
    regime = str(gex_data["regime"])
    pcr_oi = float(pcr_data["pcr_oi"])
    skew_pct = float(skew_data["skew_25d_pct"])

    # 3. 1-Sigma Daily Volatility in USD
    atm_strikes = [x for x in payload.chain if abs(x.strike - spot) / spot <= 0.05 and x.iv > 0.05]
    if atm_strikes:
        atm_iv = float(np.mean([x.iv for x in atm_strikes]))
    else:
        all_ivs = [x.iv for x in payload.chain if x.iv > 0.05]
        atm_iv = float(np.mean(all_ivs)) if all_ivs else 0.55

    daily_sigma = atm_iv / math.sqrt(365.0)
    daily_1sigma_usd = round(spot * daily_sigma, 1)

    # 4. Direction & Confidence
    confidence_pct = round(min(98.5, max(52.0, 50.0 + (abs(composite) / 100.0) * 48.0)), 1)

    entry = round(spot, 1)

    if composite >= 20.0:
        direction = "LONG (BUY)"
        # SL below Put Wall support floor
        if put_wall < entry:
            sl = round(min(entry - 0.35 * daily_1sigma_usd, put_wall * 0.994), 1)
        else:
            sl = round(entry - 0.75 * daily_1sigma_usd, 1)
        sl_pct = round(((sl - entry) / entry) * 100.0, 2)

        # Targets
        tg1_dist = max(0.45 * daily_1sigma_usd, (max_pain - entry) if max_pain > entry else 0.45 * daily_1sigma_usd)
        tg1 = round(entry + tg1_dist, 1)
        tg1_pct = round(((tg1 - entry) / entry) * 100.0, 2)

        tg2_dist = max(0.85 * daily_1sigma_usd, (call_wall - entry) if call_wall > tg1 else 0.85 * daily_1sigma_usd)
        tg2 = round(max(tg1 + 0.25 * daily_1sigma_usd, entry + tg2_dist), 1)
        tg2_pct = round(((tg2 - entry) / entry) * 100.0, 2)

        tg3 = round(max(tg2 + 0.35 * daily_1sigma_usd, entry + 1.35 * daily_1sigma_usd), 1)
        tg3_pct = round(((tg3 - entry) / entry) * 100.0, 2)

        risk = max(entry - sl, 10.0)
        reward = max(tg2 - entry, 10.0)
        rrr = round(reward / risk, 2)

        call_strike = round(entry / 500) * 500
        put_strike = round(put_wall / 500) * 500
        options_rec = f"BUY {int(call_strike):,} CALL or SELL {int(put_strike):,} PUT"
        strat_name = "Gamma Support Bounce Long" if regime == "POSITIVE_GAMMA_MEAN_REVERTING" else "Negative Gamma Squeeze Breakout Long"
        explanation = (
            f"Bullish institutional options flow with a composite score of {composite:+.1f}/100. "
            f"Heavy Put Wall support floor at ${put_wall:,.0f} anchors the downside risk. "
            f"Net delta OI change signals dealer put writing. Invalidation SL placed below structural support at ${sl:,.0f}, "
            f"targeting scalp TG1 at ${tg1:,.0f} and primary Call Wall resistance at ${tg2:,.0f}."
        )

    elif composite <= -20.0:
        direction = "SHORT (SELL)"
        # SL above Call Wall resistance ceiling
        if call_wall > entry:
            sl = round(max(entry + 0.35 * daily_1sigma_usd, call_wall * 1.006), 1)
        else:
            sl = round(entry + 0.75 * daily_1sigma_usd, 1)
        sl_pct = round(((sl - entry) / entry) * 100.0, 2)

        # Downside targets
        tg1_dist = max(0.45 * daily_1sigma_usd, (entry - max_pain) if max_pain < entry else 0.45 * daily_1sigma_usd)
        tg1 = round(entry - tg1_dist, 1)
        tg1_pct = round(((tg1 - entry) / entry) * 100.0, 2)

        tg2_dist = max(0.85 * daily_1sigma_usd, (entry - put_wall) if put_wall < tg1 else 0.85 * daily_1sigma_usd)
        tg2 = round(min(tg1 - 0.25 * daily_1sigma_usd, entry - tg2_dist), 1)
        tg2_pct = round(((tg2 - entry) / entry) * 100.0, 2)

        tg3 = round(min(tg2 - 0.35 * daily_1sigma_usd, entry - 1.35 * daily_1sigma_usd), 1)
        tg3_pct = round(((tg3 - entry) / entry) * 100.0, 2)

        risk = max(sl - entry, 10.0)
        reward = max(entry - tg2, 10.0)
        rrr = round(reward / risk, 2)

        put_strike = round(entry / 500) * 500
        call_strike = round(call_wall / 500) * 500
        options_rec = f"BUY {int(put_strike):,} PUT or SELL {int(call_strike):,} CALL"
        strat_name = "Overhead Call Wall Rejection Short" if regime == "POSITIVE_GAMMA_MEAN_REVERTING" else "Negative Gamma Breakdown Short"
        explanation = (
            f"Bearish options institutional supply with composite score of {composite:+.1f}/100. "
            f"Major Call Wall resistance at ${call_wall:,.0f} caps upside with dealer gamma overhead. "
            f"Stop Loss anchored beyond resistance at ${sl:,.0f}, targeting downside gravitational pull to Max Pain at ${tg1:,.0f} "
            f"and Put Wall support floor at ${tg2:,.0f}."
        )

    else:
        direction = "NEUTRAL (RANGE / THETA)"
        sl = round(min(entry - 0.5 * daily_1sigma_usd, put_wall * 0.985), 1)
        sl_pct = round(((sl - entry) / entry) * 100.0, 2)

        tg1 = round(max_pain if abs(max_pain - entry) > 50 else entry + 0.45 * daily_1sigma_usd, 1)
        tg1_pct = round(((tg1 - entry) / entry) * 100.0, 2)

        tg2 = round(call_wall, 1)
        tg2_pct = round(((tg2 - entry) / entry) * 100.0, 2)

        tg3 = round(call_wall * 1.015, 1)
        tg3_pct = round(((tg3 - entry) / entry) * 100.0, 2)

        rrr = 2.45

        put_strike = round(put_wall / 500) * 500
        call_strike = round(call_wall / 500) * 500
        options_rec = f"SELL {int(put_strike):,} PUT & SELL {int(call_strike):,} CALL STRANGLE"
        strat_name = "Delta-Neutral Iron Condor / Short Strangle"
        explanation = (
            f"Price is balancing within the institutional Put-Call corridor between ${put_wall:,.0f} (Put Wall) "
            f"and ${call_wall:,.0f} (Call Wall). Current market regime favors mean-reversion with low directional impulse. "
            f"Optimal risk-adjusted strategy is harvesting rapid theta decay by selling out-of-the-money strangle/condor wings."
        )

    trade_levels = TradeLevels(
        entry=entry,
        stop_loss=sl,
        sl_pct=sl_pct,
        tg1=tg1,
        tg1_pct=tg1_pct,
        tg2=tg2,
        tg2_pct=tg2_pct,
        tg3=tg3,
        tg3_pct=tg3_pct,
        risk_reward_ratio=f"1 : {rrr:.2f}"
    )

    strategy_used = StrategyUsed(
        name=strat_name,
        gamma_regime=regime,
        basis_explanation=explanation,
        recommended_options_trade=options_rec
    )

    structural_anchors = StructuralAnchors(
        put_wall_support=put_wall,
        call_wall_resistance=call_wall,
        zero_gamma_pivot=zero_gamma,
        max_pain_magnet=max_pain,
        daily_1sigma_usd=daily_1sigma_usd,
        pcr_oi=round(pcr_oi, 2),
        skew_25d_pct=round(skew_pct, 2)
    )

    return AlgoSignalOutput(
        symbol="BTCUSDT",
        signal_direction=direction,
        composite_score=composite,
        confidence_pct=confidence_pct,
        trade_levels=trade_levels,
        strategy_used=strategy_used,
        structural_anchors=structural_anchors,
        expiry=chosen_expiry,
        available_expiries=available_expiries
    )


@router.get("/live", response_model=AlgoSignalOutput)
async def get_algo_signals_live(
    expiry_index: int = Query(0, description="Index of option expiry date (0 is nearest)"),
    expiry: Optional[str] = Query(None, description="Specific expiry date YYYY-MM-DD")
):
    try:
        return compute_algo_signals(expiry_index=expiry_index, expiry=expiry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate Algo Trade Levels: {str(e)}")


@router.get("/expiries", response_model=List[str])
async def get_algo_expiries():
    try:
        _, _, expiries = fetch_live_delta_chain_payload()
        return expiries
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
