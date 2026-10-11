import asyncio
import json
import logging
import time
from collections import Counter, OrderedDict


class Cache:
    def __init__(self, url: str, ttl: int, *, l1_size=256, l1_ttl=10, monitoring=None):
        self.ttl = ttl
        self.l1_size, self.l1_ttl = l1_size, min(l1_ttl, ttl)
        self.local = OrderedDict()
        self.monitoring = monitoring
        self.counts = Counter()
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.redis = None
        self.cipher = None
        if url:
            from redis.asyncio import Redis

            self.redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)

    async def _decode(self, value, key):
        if self.cipher:
            return await asyncio.to_thread(self.cipher.decrypt, value, key)
        return await asyncio.to_thread(json.loads, value)

    def event(self, key, tier, result):
        namespace = "cypher" if "cypher:" in key else "rerank" if "rerank:" in key else "retrieval"
        self.counts[f"{tier}_{result}"] += 1
        if self.monitoring:
            self.monitoring.cache_events.labels(namespace, tier, result).inc()

    def remember(self, key, value, ttl):
        if len(value) > 1024 * 1024 or ttl <= 0:
            return
        self.local[key] = (time.monotonic() + min(ttl, self.l1_ttl), value)
        self.local.move_to_end(key)
        while len(self.local) > self.l1_size:
            self.local.popitem(last=False)
            self.event(key, "l1", "eviction")

    async def get(self, key):
        item = self.local.get(key)
        if item and item[0] > time.monotonic():
            self.local.move_to_end(key)
            self.event(key, "l1", "hit")
            return await self._decode(item[1], key)
        self.local.pop(key, None)
        self.event(key, "l1", "miss")
        if self.redis:
            try:
                value = await self.redis.get(key)
                if value is not None:
                    remaining = await self.redis.pttl(key)
                    decoded = await self._decode(value, key)
                    self.remember(key, value, max(0, remaining / 1000))
                    self.event(key, "l2", "hit")
                    return decoded
                self.event(key, "l2", "miss")
            except Exception:
                self.event(key, "l2", "error")
                logging.getLogger(__name__).warning("redis_cache_unavailable")
        return None

    async def set(self, key, value):
        encoded = (
            await asyncio.to_thread(self.cipher.encrypt, value, key)
            if self.cipher
            else await asyncio.to_thread(json.dumps, value, ensure_ascii=False)
        )
        self.remember(key, encoded, self.ttl)
        if self.redis:
            try:
                await self.redis.set(key, encoded, ex=self.ttl)
            except Exception:
                self.event(key, "l2", "error")
                logging.getLogger(__name__).warning("redis_cache_unavailable")

    async def get_or_load(self, key, loader):
        result = await self.get(key)
        if result is not None:
            return result
        async with self.locks[hash(key) % len(self.locks)]:
            result = await self.get(key)
            if result is None:
                result = await loader()
                await self.set(key, result)
            return result

    def stats(self):
        return {
            "l1_size": len(self.local),
            "l1_capacity": self.l1_size,
            "l1_ttl": self.l1_ttl,
            "l2_enabled": self.redis is not None,
            "l2_ttl": self.ttl,
            "events": dict(self.counts),
        }

    async def close(self):
        if self.redis:
            await self.redis.aclose()
