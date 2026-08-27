from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if a query contains privacy-sensitive content."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True when both queries contain different four-digit values."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """TTL-bounded in-memory semantic response cache with safety guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Return the closest safe cache hit and its similarity score."""
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        self._entries = [
            entry for entry in self._entries if now - entry.created_at <= self.ttl_seconds
        ]

        best_entry: CacheEntry | None = None
        best_score = 0.0
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_entry.key,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score
            return best_entry.value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a cacheable response with insertion time and metadata."""
        if _is_uncacheable(query):
            return
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=dict(metadata or {}),
            )
        )

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute cosine similarity over word tokens and character trigrams."""
        if a == b:
            return 1.0

        def tokenize(text: str) -> list[str]:
            tokens: list[str] = []
            for word in re.findall(r"\w+", text.lower()):
                tokens.append(word)
                if len(word) >= 3:
                    tokens.extend(word[index : index + 3] for index in range(len(word) - 2))
            return tokens

        vector_a = Counter(tokenize(a))
        vector_b = Counter(tokenize(b))
        if not vector_a or not vector_b:
            return 0.0

        dot_product = sum(
            count * vector_b.get(token, 0) for token, count in vector_a.items()
        )
        norm_a = math.sqrt(sum(count * count for count in vector_a.values()))
        norm_b = math.sqrt(sum(count * count for count in vector_b.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot_product / (norm_a * norm_b)


class SharedRedisCache:
    """Redis-backed semantic cache shared by multiple gateway instances."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Return an exact or semantically similar safe Redis cache hit."""
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact_response = self._redis.hget(exact_key, "response")
        if exact_response is not None:
            return str(exact_response), 1.0

        best_query: str | None = None
        best_response: str | None = None
        best_score = 0.0
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(key, "query")
            if cached_query is None:
                continue
            cached_query_text = str(cached_query)
            score = ResponseCache.similarity(query, cached_query_text)
            if score > best_score:
                best_score = score
                best_query = cached_query_text
                response = self._redis.hget(key, "response")
                best_response = None if response is None else str(response)

        if (
            best_query is not None
            and best_response is not None
            and best_score >= self.similarity_threshold
        ):
            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_query,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score
            return best_response, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a cacheable response in Redis and attach a TTL."""
        del metadata
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries belonging to this cache prefix."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Return a deterministic short hash for a normalized query."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
