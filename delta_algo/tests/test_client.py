"""Basic tests for API client (no real API needed)."""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__),".."))

from api.client import DeltaClient, DeltaAPIError, RateLimitTracker
from utils.options_math import bs_price, bs_delta, days_to_expiry, iv_percentile
import pandas as pd

def test_signature_generation():
    client = DeltaClient("https://test.example.com","key123","secret456")
    ts, sig = client._sign("POST","/v2/orders","","{}")
    assert len(sig) == 64
    assert int(ts) > 0

def test_rate_limit_tracker():
    rl = RateLimitTracker(quota=100)
    for _ in range(20): rl.consume("place_order")  # 20*5=100
    assert rl.remaining == 0
    assert not rl.consume("place_order")

def test_bs_price():
    p = bs_price(50000, 52000, 7/365, 0.05, 0.60, "call")
    assert p > 0
    put_p = bs_price(50000, 48000, 7/365, 0.05, 0.60, "put")
    assert put_p > 0
    intrinsic = max(0, 50000 - 48000)
    assert put_p >= intrinsic * 0.99

def test_bs_delta():
    # OTM call delta should be between 0 and 0.5
    d = bs_delta(50000, 55000, 7/365, 0.05, 0.60, "call")
    assert 0 < d < 0.5
    # OTM put delta should be between -0.5 and 0
    d = bs_delta(50000, 45000, 7/365, 0.05, 0.60, "put")
    assert -0.5 < d < 0

def test_iv_percentile():
    series = pd.Series([0.2, 0.3, 0.4, 0.5, 0.6])
    pct = iv_percentile(series, 0.45)
    assert 50 <= pct <= 90

def test_days_to_expiry():
    from datetime import date
    # Future date
    future = date.today()
    future_str = future.strftime("%d%m") + str(future.year)[-2:]
    # Just test parsing doesn't crash
    dte = days_to_expiry(f"C-BTC-90000-{future_str}")
    assert dte >= 0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
