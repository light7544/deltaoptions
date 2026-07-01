"""Load and validate config.yaml."""
import os
import yaml
from dotenv import load_dotenv

def load_config(path: str = None) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    load_dotenv(env_path) # Load variables from .env
    
    if path is None:
        # Look relative to project root
        path = os.path.join(here, "..", "config", "config.yaml")
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
        
    # Override credentials securely from environment variables
    env_key = os.getenv("DELTA_API_KEY")
    env_secret = os.getenv("DELTA_API_SECRET")
    
    if "exchange" not in cfg:
        cfg["exchange"] = {}
        
    env_env = os.getenv("DELTA_ENV")
    if env_env: cfg["exchange"]["environment"] = env_env
        
    if env_key: cfg["exchange"]["api_key"] = env_key
    if env_secret: cfg["exchange"]["api_secret"] = env_secret
        
    _validate(cfg)
    return cfg

def _validate(cfg: dict):
    required = [
        ("exchange", "api_key"),
        ("exchange", "api_secret"),
        ("exchange", "environment"),
    ]
    for section, key in required:
        val = cfg.get(section, {}).get(key, "")
        if not val or "YOUR_" in str(val):
            raise ValueError(
                f"config.yaml: [{section}][{key}] is not set. "
                f"Please fill in your credentials."
            )
