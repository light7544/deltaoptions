"""
Delta Exchange API Client — Production Grade
"""
import hashlib, hmac, json, logging, time
from typing import Any, Dict, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

class DeltaAPIError(Exception):
    def __init__(self, code="", message="", context=None):
        self.code = code; self.message = message; self.context = context or {}
        super().__init__(f"DeltaAPIError [{code}]: {message}")

class DeltaRateLimitError(DeltaAPIError): pass
class DeltaAuthError(DeltaAPIError): pass
class DeltaInsufficientMarginError(DeltaAPIError): pass

class RateLimitTracker:
    WEIGHTS = {"place_order":5,"edit_order":5,"cancel_order":5,"get_orders":3,
               "get_positions":3,"get_balance":3,"get_tickers":3,"get_products":3,
               "get_orderbook":3,"batch_orders":25,"ohlc_candles":3,"default":1}
    def __init__(self, quota=9000):
        self.quota = quota; self.used = 0; self.window_start = time.time()
    def _reset(self):
        if time.time() - self.window_start >= 300:
            self.used = 0; self.window_start = time.time()
    def consume(self, key="default"):
        self._reset()
        w = self.WEIGHTS.get(key, 1)
        if self.used + w > self.quota:
            logger.warning(f"Quota near limit: {self.used}/{self.quota}")
            return False
        self.used += w
        return True
    @property
    def remaining(self): self._reset(); return self.quota - self.used

class DeltaClient:
    def __init__(self, base_url, api_key, api_secret, user_agent="delta-algo-v1",
                 max_retries=3, retry_delay=2.0, quota=9000, timeout=(5,30)):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key; self.api_secret = api_secret
        self.user_agent = user_agent; self.max_retries = max_retries
        self.retry_delay = retry_delay; self.timeout = timeout
        self.rate_limiter = RateLimitTracker(quota)
        self.session = self._build_session()

    @classmethod
    def from_config(cls, cfg):
        env = str(cfg["exchange"]["environment"]).lower()
        base_url = cfg["exchange"]["base_url_production"] if env in ("production", "live") else cfg["exchange"]["base_url_testnet"]
        return cls(base_url=base_url, api_key=cfg["exchange"]["api_key"],
                   api_secret=cfg["exchange"]["api_secret"],
                   user_agent=cfg["exchange"].get("user_agent","delta-algo"),
                   max_retries=cfg["orders"].get("max_retries",3),
                   retry_delay=cfg["orders"].get("retry_delay_seconds",2.0),
                   quota=cfg["rate_limits"].get("quota_per_5min",9000))

    def _build_session(self):
        s = requests.Session()
        s.mount("https://", HTTPAdapter(max_retries=Retry(total=0,connect=2,read=2,backoff_factor=1)))
        return s

    def _sign(self, method, path, qs="", body=""):
        ts = str(int(time.time()))
        msg = method.upper() + ts + path + qs + body
        sig = hmac.new(bytes(self.api_secret,"utf-8"),bytes(msg,"utf-8"),hashlib.sha256).hexdigest()
        return ts, sig

    def _headers(self, method, path, qs="", body="", auth=True):
        h = {"User-Agent":self.user_agent,"Content-Type":"application/json","Accept":"application/json"}
        if auth:
            ts, sig = self._sign(method, path, qs, body)
            h.update({"api-key":self.api_key,"timestamp":ts,"signature":sig})
        return h

    def _request(self, method, path, params=None, body=None, auth=True, key="default"):
        url = f"{self.base_url}{path}"
        body_str = json.dumps(body, separators=(",",":")) if body else ""
        qs = ""
        if params:
            import urllib.parse
            qs = "?" + urllib.parse.urlencode(params)

        for attempt in range(1, self.max_retries+1):
            if not self.rate_limiter.consume(key):
                time.sleep(60)
            headers = self._headers(method, path, qs, body_str, auth)
            try:
                logger.debug(f"→ {method} {path} {params}")
                r = self.session.request(method.upper(), url, headers=headers,
                    params=params, data=body_str if body else None, timeout=self.timeout)
                if r.status_code == 429:
                    wait = max(int(r.headers.get("X-RATE-LIMIT-RESET",5000))/1000, 5)
                    logger.warning(f"429 rate limit — sleeping {wait}s"); time.sleep(wait)
                    raise DeltaRateLimitError("rate_limit_exceeded")
                if r.status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay*attempt); continue
                    r.raise_for_status()
                data = r.json()
                if not data.get("success", True):
                    err = data.get("error", {}); code = err.get("code","unknown")
                    ctx = err.get("context", {}); msg = str(err)
                    if code in ("InvalidApiKey","SignatureExpired","UnauthorizedApiAccess","Signature Mismatch"):
                        raise DeltaAuthError(code, msg, ctx)
                    elif code == "insufficient_margin":
                        raise DeltaInsufficientMarginError(code, msg, ctx)
                    else:
                        raise DeltaAPIError(code, msg, ctx)
                result = data.get("result", data)
                logger.debug(f"← {method} {path} OK")
                return result
            except (DeltaRateLimitError, DeltaAuthError, DeltaInsufficientMarginError): raise
            except DeltaAPIError as e:
                if attempt < self.max_retries:
                    logger.warning(f"API err {e.code} attempt {attempt}"); time.sleep(self.retry_delay*attempt); continue
                raise
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay*attempt); continue
                raise DeltaAPIError("network_error", str(e))

    # ── Public ──────────────────────────────────────
    def get_assets(self): return self._request("GET","/v2/assets",auth=False)
    def get_products(self,contract_types=None,states="live",expiry=None,page_size=100):
        p={"states":states,"page_size":page_size}
        if contract_types: p["contract_types"]=contract_types
        if expiry: p["expiry"]=expiry
        return self._request("GET","/v2/products",params=p,auth=False,key="get_products")
    def get_product_by_symbol(self,sym): return self._request("GET",f"/v2/products/{sym}",auth=False,key="get_products")
    def get_tickers(self,contract_types=None,underlying=None,expiry_date=None):
        p={}
        if contract_types: p["contract_types"]=contract_types
        if underlying: p["underlying_asset_symbols"]=underlying
        if expiry_date: p["expiry_date"]=expiry_date
        return self._request("GET","/v2/tickers",params=p,auth=False,key="get_tickers")
    def get_ticker(self,sym): return self._request("GET",f"/v2/tickers/{sym}",auth=False,key="get_tickers")
    def get_option_chain(self,underlying,expiry_date):
        return self.get_tickers("call_options,put_options",underlying,expiry_date)
    def get_orderbook(self,sym,depth=5):
        return self._request("GET",f"/v2/l2orderbook/{sym}",params={"depth":depth},auth=False,key="get_orderbook")
    def get_ohlc(self,sym,resolution,start,end):
        return self._request("GET","/v2/history/candles",params={"symbol":sym,"resolution":resolution,"start":start,"end":end},auth=False,key="ohlc_candles")

    # ── Private ─────────────────────────────────────
    def get_wallet_balances(self):
        r = self._request("GET","/v2/wallet/balances",key="get_balance")
        return r.get("result",r) if isinstance(r,dict) else r
    def get_positions(self,product_id=None,underlying=None):
        p={}
        if product_id: p["product_id"]=product_id
        elif underlying: p["underlying_asset_symbol"]=underlying
        return self._request("GET","/v2/positions",params=p,key="get_positions")
    def get_all_positions(self,ct="call_options,put_options"):
        return self._request("GET","/v2/positions/margined",params={"contract_types":ct},key="get_positions")
    def get_active_orders(self,product_ids=None,ct="call_options,put_options",states="open,pending",page_size=50):
        p={"states":states,"page_size":page_size}
        if product_ids: p["product_ids"]=product_ids
        if ct: p["contract_types"]=ct
        return self._request("GET","/v2/orders",params=p,key="get_orders")
    def get_order(self,order_id): return self._request("GET",f"/v2/orders/{order_id}",key="get_orders")
    def get_profile(self): return self._request("GET","/v2/profile")

    def place_order(self,product_id,size,side,order_type="limit_order",limit_price=None,
                    time_in_force="gtc",post_only=False,reduce_only=False,
                    client_order_id=None,stop_order_type=None,stop_price=None):
        body={"product_id":product_id,"size":size,"side":side,"order_type":order_type,
              "time_in_force":time_in_force,"post_only":post_only,"reduce_only":reduce_only}
        if limit_price is not None: body["limit_price"]=str(limit_price)
        if client_order_id: body["client_order_id"]=client_order_id[:32]
        if stop_order_type: body["stop_order_type"]=stop_order_type
        if stop_price: body["stop_price"]=str(stop_price)
        logger.info(f"ORDER {side} {order_type} pid={product_id} size={size} price={limit_price}")
        return self._request("POST","/v2/orders",body=body,key="place_order")

    def place_market_order(self,product_id,size,side,reduce_only=False,client_order_id=None):
        return self.place_order(product_id,size,side,"market_order",reduce_only=reduce_only,client_order_id=client_order_id)

    def edit_order(self,order_id,product_id,size,limit_price=None):
        body={"id":order_id,"product_id":product_id,"size":size}
        if limit_price: body["limit_price"]=str(limit_price)
        return self._request("PUT","/v2/orders",body=body,key="edit_order")

    def cancel_order(self,order_id,product_id,client_order_id=None):
        body={"id":order_id,"product_id":product_id}
        if client_order_id: body["client_order_id"]=client_order_id
        logger.info(f"CANCEL order={order_id} pid={product_id}")
        return self._request("DELETE","/v2/orders",body=body,key="cancel_order")

    def cancel_all_orders(self,product_id=None,ct="call_options,put_options"):
        body={"cancel_limit_orders":True}
        if product_id: body["product_id"]=product_id
        else: body["contract_types"]=ct
        logger.warning(f"CANCEL ALL ORDERS ct={ct}")
        return self._request("DELETE","/v2/orders/all",body=body)

    def batch_place_orders(self,product_id,orders):
        return self._request("POST","/v2/orders/batch",body={"product_id":product_id,"orders":orders},key="batch_orders")

    def close_position(self,product_id):
        pos = self.get_positions(product_id=product_id)
        size = abs(int(pos.get("size",0)))
        if size==0: return {}
        side = "buy" if int(pos.get("size",0))<0 else "sell"
        return self.place_market_order(product_id,size,side,reduce_only=True)

    def close_all_positions(self):
        logger.warning("CLOSING ALL POSITIONS")
        return self._request("POST","/v2/positions/close_all",
            body={"close_all_portfolio":True,"close_all_isolated":True,"user_id":0})

    def set_leverage(self,product_id,leverage):
        return self._request("POST",f"/v2/products/{product_id}/orders/leverage",body={"leverage":leverage})

    def create_heartbeat(self,hb_id,ct=None,action="cancel_orders",unhealthy_count=1):
        body={"heartbeat_id":hb_id,"impact":"contracts",
              "contract_types":ct or ["call_options","put_options"],
              "config":[{"action":action,"unhealthy_count":unhealthy_count}]}
        return self._request("POST","/v2/heartbeat/create",body=body)

    def send_heartbeat(self,hb_id,ttl_ms=30000):
        return self._request("POST","/v2/heartbeat",body={"heartbeat_id":hb_id,"ttl":ttl_ms})

    def ping(self):
        try: self.get_assets(); return True
        except: return False

    def __repr__(self): return f"DeltaClient(url={self.base_url}, quota_left={self.rate_limiter.remaining})"
