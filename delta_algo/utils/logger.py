"""Logging setup — file + console, daily rotation."""
import logging, logging.handlers, os, sys
from datetime import datetime

def setup_logger(name="delta_algo", cfg: dict = None) -> logging.Logger:
    cfg = cfg or {}
    level_str = cfg.get("level", "INFO")
    level = getattr(logging, level_str.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger(name)
    root.setLevel(level)
    root.handlers.clear()

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level); ch.setFormatter(fmt)
    root.addHandler(ch)

    # File handler
    if cfg.get("log_to_file", True):
        log_dir = cfg.get("log_dir", "logs")
        os.makedirs(log_dir, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        log_file = os.path.join(log_dir, f"algo_{today}.log")
        fh = logging.handlers.TimedRotatingFileHandler(
            log_file, when="midnight", interval=1, backupCount=30
        )
        fh.setLevel(level); fh.setFormatter(fmt)
        root.addHandler(fh)

    return root
