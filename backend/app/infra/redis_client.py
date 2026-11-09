from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit

from redis import Redis

from app.config import get_settings


@lru_cache(maxsize=4)
def get_redis_client(url: str | None = None) -> Redis:
    settings = get_settings()
    target = url or settings.redis_url
    # Celery/Kombu and redis-py name the same Unix transport differently.
    # Preserve the selected database; probing db=0 for every broker is misleading.
    if target.startswith("redis+socket://"):
        parts = urlsplit(target)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if sum(key == "virtual_host" for key, _ in query) > 1 or any(key == "db" for key, _ in query):
            raise ValueError("REDIS_SOCKET_DATABASE_AMBIGUOUS")
        query = [("db" if key == "virtual_host" else key, value) for key, value in query]
        target = f"unix://{parts.netloc}{parts.path}?{urlencode(query)}"
    return Redis.from_url(
        target,
        decode_responses=False,
        socket_connect_timeout=0.1,
        socket_timeout=0.2,
    )
