"""Base Strategy Interface."""
from abc import ABC, abstractmethod

class BaseStrategy(ABC):
    def __init__(self, client, cfg, risk_manager, trade_logger):
        self.client = client
        self.cfg = cfg
        self.risk = risk_manager
        self.tlog = trade_logger

    @abstractmethod
    def scan_entries(self, underlying: str) -> list:
        """Scan for entry signals. Returns list of trade setups."""
        pass

    @abstractmethod
    def check_exits(self, positions: list) -> list:
        """Check all open positions for exit conditions."""
        pass

    @abstractmethod
    def execute_entry(self, setup: dict) -> dict:
        """Execute entry orders for a given setup."""
        pass

    @abstractmethod
    def execute_exit(self, position: dict, reason: str) -> dict:
        """Execute exit orders for a given position."""
        pass
