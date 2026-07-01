"""Strategy factory — returns the configured strategy."""
from strategies.short_strangle import ShortStrangleStrategy
from strategies.iron_condor import IronCondorStrategy

def get_strategy(name: str, client, cfg, risk_manager, trade_logger):
    strategies = {
        "short_strangle": ShortStrangleStrategy,
        "iron_condor":    IronCondorStrategy,
    }
    cls = strategies.get(name)
    if not cls:
        raise ValueError(f"Unknown strategy: {name}. Choose from: {list(strategies.keys())}")
    return cls(client, cfg, risk_manager, trade_logger)
