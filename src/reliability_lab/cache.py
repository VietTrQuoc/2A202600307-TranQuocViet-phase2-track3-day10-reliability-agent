from __future__ import annotations

import hashlib
import json
import re
import string
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities - use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _normalize(query: str) -> str:
    """Normalize a query for deterministic exact-match and similarity checks."""
    cleaned = query.lower().translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    return " ".join(cleaned.split())


def _tokens(query: str) -> list[str]:
    """Tokenize a query after normalization."""
    normalized = _normalize(query)
    return normalized.split() if normalized else []


def _char_ngrams(query: str, n: int = 3) -> set[str]:
    """Return character n-grams for short deterministic fuzzy matching."""
    normalized = _normalize(query).replace(" ", "")
    if len(normalized) < n:
        return {normalized} if normalized else set()
    return {normalized[i : i + n] for i in range(len(normalized) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    """Calculate Jaccard similarity for two sets."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory cache with TTL, similarity search, and safety guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []
        self._lock = threading.RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        with self._lock:
            best_value: str | None = None
            best_key: str | None = None
            best_score = 0.0
            now = time.time()
            self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
            for entry in self._entries:
                if _is_uncacheable(entry.key):
                    continue
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_value = entry.value
                    best_key = entry.key
            if best_score >= self.similarity_threshold:
                if best_key is not None and _looks_like_false_hit(query, best_key):
                    self.false_hit_log.append(
                        {"query": query, "cached_query": best_key, "score": round(best_score, 4)}
                    )
                    return None, best_score
                return best_value, best_score
            return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        with self._lock:
            self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Deterministic lexical similarity using token and character features."""
        if _normalize(a) == _normalize(b):
            return 1.0

        left_tokens = _tokens(a)
        right_tokens = _tokens(b)
        if not left_tokens or not right_tokens:
            return 0.0

        left_counts = Counter(left_tokens)
        right_counts = Counter(right_tokens)
        shared = set(left_counts) & set(right_counts)
        numerator = sum(left_counts[token] * right_counts[token] for token in shared)
        left_norm = sum(count * count for count in left_counts.values()) ** 0.5
        right_norm = sum(count * count for count in right_counts.values()) ** 0.5
        cosine = numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0

        token_jaccard = _jaccard(set(left_tokens), set(right_tokens))
        ngram_jaccard = _jaccard(_char_ngrams(a), _char_ngrams(b))
        return (0.55 * cosine) + (0.30 * token_jaccard) + (0.15 * ngram_jaccard)


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

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
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        if _is_uncacheable(query):
            return None, 0.0

        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if isinstance(exact_response, str):
                return exact_response, 1.0

            best_response: str | None = None
            best_query: str | None = None
            best_score = 0.0
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                cached_response = self._redis.hget(key, "response")
                if not isinstance(cached_query, str) or not isinstance(cached_response, str):
                    continue
                if _is_uncacheable(cached_query):
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_query = cached_query
                    best_response = cached_response

            if best_score >= self.similarity_threshold:
                if best_query is not None and _looks_like_false_hit(query, best_query):
                    self.false_hit_log.append(
                        {"query": query, "cached_query": best_query, "score": round(best_score, 4)}
                    )
                    return None, best_score
                return best_response, best_score
            return None, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            mapping = {
                "query": query,
                "response": value,
                "metadata": json.dumps(metadata or {}, sort_keys=True),
            }
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix."""
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except Exception:
            return

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            try:
                self._redis.close()
            except Exception:
                return

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
