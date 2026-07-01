import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from utils.config_loader import load_config
from api.client import DeltaClient

cfg = load_config()
client = DeltaClient.from_config(cfg)
try:
    print("Testing /api/positions equivalent...")
    res = client._request("GET", "/v2/positions/margined", params={"contract_types": "call_options,put_options"})
    print("Success:", res)
except Exception as e:
    print("Error:", repr(e))
