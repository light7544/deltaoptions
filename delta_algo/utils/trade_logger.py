"""CSV trade logger for all filled orders."""
import csv, os, logging
from datetime import datetime

logger = logging.getLogger(__name__)

FIELDS = ["timestamp","trade_id","underlying","symbol","product_id","side",
          "order_type","size","entry_price","strategy","leg","status","pnl_usd","notes"]

class TradeLogger:
    def __init__(self, filepath: str = "logs/trades.csv"):
        self.filepath = filepath
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if not os.path.exists(filepath):
            with open(filepath, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log(self, **kwargs):
        row = {f: kwargs.get(f, "") for f in FIELDS}
        row["timestamp"] = row.get("timestamp") or datetime.utcnow().isoformat()
        with open(self.filepath, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        logger.info(f"TRADE LOG: {row}")
