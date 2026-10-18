"""
gamma_exposure_map.py
---------------------
Gamma Exposure Map & Implied Daily Range Predictor Service.
Provides live endpoint GET /api/v1/gamma-map/live?expiry_index=0
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

router = APIRouter(prefix="/api/v1/gamma-map", tags=["Gamma Exposure Map"])


class DailyRangeData(BaseModel):
    expected_move_pct: float
    low_1sd: float
    high_1sd: float
    atm_iv_pct: float
    move_probabilities: Dict[str, float]


class GammaMapOutput(BaseModel):
    symbol: str = "BTCUSDT"
    spot_price: float
    gamma_wall_strike: float
    put_wall_strike: float
    zero_gamma_flip: float
    net_gex_m: float
    regime: str
    upside_pct: float
    downside_pct: float
    spot_relative_pct: float  # 0% = at Put Wall, 100% = at Gamma Wall
    y_axis_ladder: List[float]
    daily_range: DailyRangeData
    expiry: str
    available_expiries: List[str]


def compute_gamma_map_data(expiry_index: int = 0, expiry: Optional[str] = None) -> GammaMapOutput:
    # 1. Fetch live payload from Delta Exchange
    payload, chosen_expiry, available_expiries = fetch_live_delta_chain_payload(expiry)
    
    # If expiry_index was specified and within range
    if expiry is None and 0 <= expiry_index < len(available_expiries):
        target_exp = available_expiries[expiry_index]
        if target_exp != chosen_expiry:
            payload, chosen_expiry, available_expiries = fetch_live_delta_chain_payload(target_exp)

    # 2. Run Quantitative Engine
    engine = BTCOptionsQuantEngine(payload)
    oi_data = engine.calculate_oi_walls_and_flow()
    gex_data = engine.calculate_net_gex()

    spot = float(payload.spot_price)
    gamma_wall = float(oi_data["call_wall_resistance"])
    put_wall = float(oi_data["put_wall_support"])
    zero_flip = float(gex_data["zero_gamma_flip_level"])
    net_gex_m = float(gex_data["net_gex_usd_m"])
    regime = str(gex_data["regime"])

    # Ensure gamma_wall > put_wall
    if gamma_wall <= put_wall:
        gamma_wall = round(spot * 1.05, 1)
        put_wall = round(spot * 0.95, 1)

    # Calculate upside and downside percentages
    upside_pct = round(((gamma_wall - spot) / spot) * 100.0, 2)
    downside_pct = round(((spot - put_wall) / spot) * 100.0, 2)

    # Spot relative position in channel (0% at Put Wall, 100% at Gamma Wall)
    channel_width = max(gamma_wall - put_wall, 1.0)
    relative_pct = ((spot - put_wall) / channel_width) * 100.0
    relative_pct = round(float(np.clip(relative_pct, 0.0, 100.0)), 2)

    # Y-axis ladder: 6 evenly spaced price levels from top (gamma_wall * 1.01) to bottom (put_wall * 0.99)
    top_lvl = gamma_wall * 1.01
    bot_lvl = put_wall * 0.99
    ladder = [round(float(v), 1) for v in np.linspace(top_lvl, bot_lvl, 6)]

    # 3. Calculate Daily Range and Move Probabilities from ATM IV
    # Filter ATM strikes within +/- 5% of spot
    atm_strikes = [x for x in payload.chain if abs(x.strike - spot) / spot <= 0.05 and x.iv > 0.05]
    if atm_strikes:
        atm_iv = float(np.mean([x.iv for x in atm_strikes]))
    else:
        # Fallback to mean IV of entire chain or 0.55
        all_ivs = [x.iv for x in payload.chain if x.iv > 0.05]
        atm_iv = float(np.mean(all_ivs)) if all_ivs else 0.55

    # 1-day standard deviation: sigma_1d = ATM_IV / sqrt(365)
    daily_sigma = atm_iv / math.sqrt(365.0)
    expected_move_pct = round(daily_sigma * 100.0, 2)

    low_1sd = round(spot * (1.0 - daily_sigma), 1)
    high_1sd = round(spot * (1.0 + daily_sigma), 1)

    # Two-tailed probability of reaching or exceeding move threshold k%
    # P(|Return| >= k%) = erfc( (k / 100) / (daily_sigma * sqrt(2)) ) * 100
    target_moves = [1.0, 2.0, 3.0, 5.0, 8.0, 10.0]
    move_probs: Dict[str, float] = {}
    for k in target_moves:
        z = (k / 100.0) / (daily_sigma * math.sqrt(2.0))
        prob = math.erfc(z) * 100.0
        # Clean rounding
        if prob < 0.1:
            p_val = round(prob, 2)
        else:
            p_val = round(prob, 1)
        move_probs[f"{int(k)}%"] = p_val

    daily_range = DailyRangeData(
        expected_move_pct=expected_move_pct,
        low_1sd=low_1sd,
        high_1sd=high_1sd,
        atm_iv_pct=round(atm_iv * 100.0, 2),
        move_probabilities=move_probs
    )

    return GammaMapOutput(
        symbol="BTCUSDT",
        spot_price=spot,
        gamma_wall_strike=gamma_wall,
        put_wall_strike=put_wall,
        zero_gamma_flip=zero_flip,
        net_gex_m=net_gex_m,
        regime=regime,
        upside_pct=upside_pct,
        downside_pct=downside_pct,
        spot_relative_pct=relative_pct,
        y_axis_ladder=ladder,
        daily_range=daily_range,
        expiry=chosen_expiry,
        available_expiries=available_expiries
    )


@router.get("/live", response_model=GammaMapOutput)
async def get_gamma_map_live(
    expiry_index: int = Query(0, description="Index of option settlement date (0 is nearest)"),
    expiry: Optional[str] = Query(None, description="Specific expiry date YYYY-MM-DD")
):
    try:
        return compute_gamma_map_data(expiry_index=expiry_index, expiry=expiry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate Gamma Map: {str(e)}")


@router.get("/expiries", response_model=List[str])
async def get_gamma_map_expiries():
    try:
        _, _, expiries = fetch_live_delta_chain_payload()
        return expiries
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
