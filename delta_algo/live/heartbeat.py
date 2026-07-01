"""Deadman Switch / Heartbeat manager — runs in background thread."""
import logging, threading, time
logger = logging.getLogger(__name__)

class HeartbeatManager:
    def __init__(self, client, cfg):
        hb = cfg["heartbeat"]
        self.client = client
        self.enabled = hb.get("enabled", True)
        self.hb_id = hb["heartbeat_id"]
        self.ttl_ms = hb["ttl_ms"]
        self.interval = hb["send_interval_seconds"]
        self.action = hb.get("action_on_miss","cancel_orders")
        self.unhealthy = hb.get("unhealthy_count",1)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self.enabled: return
        try:
            self.client.create_heartbeat(self.hb_id, action=self.action, unhealthy_count=self.unhealthy)
            logger.info(f"Heartbeat created: {self.hb_id}")
        except Exception as e:
            logger.warning(f"Heartbeat create failed (may already exist): {e}")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="heartbeat")
        self._thread.start()
        logger.info(f"Heartbeat thread started (interval={self.interval}s)")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.client.send_heartbeat(self.hb_id, self.ttl_ms)
                logger.debug(f"Heartbeat ACK sent: {self.hb_id}")
            except Exception as e:
                logger.error(f"Heartbeat send failed: {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=5)
        logger.info("Heartbeat stopped")
