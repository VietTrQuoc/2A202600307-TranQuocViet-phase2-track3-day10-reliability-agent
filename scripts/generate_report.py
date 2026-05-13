from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reliability_lab.config import load_config


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _metric(metrics: dict[str, Any], key: str, default: object = 0) -> object:
    return metrics.get(key, default)


def _met(actual: float | None, target: float, direction: str) -> str:
    if actual is None:
        return "No"
    if direction == ">=":
        return "Yes" if actual >= target else "No"
    return "Yes" if actual < target else "No"


def _pct_delta(delta: object, base: object) -> str:
    try:
        delta_f = float(delta)
        base_f = float(base)
    except (TypeError, ValueError):
        return "n/a"
    if base_f == 0:
        return _fmt(delta_f)
    return f"{(delta_f / base_f) * 100:.1f}%"


def _scenario_observation(summary: dict[str, Any]) -> str:
    routes = summary.get("route_counts", {})
    providers = summary.get("provider_counts", {})
    return (
        f"availability={_fmt(summary.get('availability'))}, "
        f"routes={routes}, providers={providers}, "
        f"circuit_open_count={_fmt(summary.get('circuit_open_count'))}, "
        f"recovery_time_ms={_fmt(summary.get('recovery_time_ms'))}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text())
    config = load_config(args.config)
    cache_comparison = metrics.get("cache_comparison", {})
    without_cache = cache_comparison.get("without_cache", {})
    with_cache = cache_comparison.get("with_cache", {})
    delta = cache_comparison.get("delta", {})
    redis_evidence = metrics.get("redis_evidence", {})
    scenario_metrics = metrics.get("scenario_metrics", {})

    recovery = metrics.get("recovery_time_ms")
    recovery_float = float(recovery) if recovery is not None else None

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        (
            "The gateway checks a safety-aware cache first, then routes cache misses through "
            "per-provider circuit breakers. If the primary provider fails or its circuit is "
            "open, traffic moves to the backup provider; if every provider is unavailable, "
            "the gateway returns a static degraded-service response."
        ),
        "",
        "```text",
        "User Request",
        "    |",
        "    v",
        "[ReliabilityGateway] -> [Cache: Redis/shared or memory] -> hit: return cached",
        "    | miss",
        "    v",
        "[CircuitBreaker: primary] -> Provider primary",
        "    | open/error",
        "    v",
        "[CircuitBreaker: backup] -> Provider backup",
        "    | open/error",
        "    v",
        "[Static fallback]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        (
            f"| failure_threshold | {config.circuit_breaker.failure_threshold} | "
            "Detects repeated provider failure quickly while tolerating isolated jitter. |"
        ),
        (
            f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | "
            "Allows recovery probes during the short local chaos run. |"
        ),
        (
            f"| success_threshold | {config.circuit_breaker.success_threshold} | "
            "One successful probe is enough for this simulated provider pool. |"
        ),
        (
            f"| cache backend | {config.cache.backend} | "
            "Redis demonstrates shared cache state; code falls back to memory when Redis is down. |"
        ),
        (
            f"| cache TTL | {config.cache.ttl_seconds} | "
            "Five minutes is long enough for FAQ reuse but short enough to limit stale answers. |"
        ),
        (
            f"| similarity_threshold | {config.cache.similarity_threshold} | "
            "High threshold favors exact or very close matches and avoids broad semantic false hits. |"
        ),
        (
            f"| load_test requests | {config.load_test.requests} | "
            "Enough volume to trigger circuit transitions and cache reuse. |"
        ),
        (
            f"| load_test concurrency | {config.load_test.concurrency} | "
            "Exercises the gateway under parallel requests without making the lab slow. |"
        ),
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        (
            f"| Availability | >= 99% | {_fmt(_metric(metrics, 'availability'))} | "
            f"{_met(float(_metric(metrics, 'availability')), 0.99, '>=')} |"
        ),
        (
            f"| Latency P95 | < 2500 ms | {_fmt(_metric(metrics, 'latency_p95_ms'))} | "
            f"{_met(float(_metric(metrics, 'latency_p95_ms')), 2500, '<')} |"
        ),
        (
            f"| Fallback success rate | >= 95% | {_fmt(_metric(metrics, 'fallback_success_rate'))} | "
            f"{_met(float(_metric(metrics, 'fallback_success_rate')), 0.95, '>=')} |"
        ),
        (
            f"| Cache hit rate | >= 10% | {_fmt(_metric(metrics, 'cache_hit_rate'))} | "
            f"{_met(float(_metric(metrics, 'cache_hit_rate')), 0.10, '>=')} |"
        ),
        (
            f"| Recovery time | < 5000 ms | {_fmt(recovery)} | "
            f"{_met(recovery_float, 5000, '<')} |"
        ),
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]

    for key in [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "estimated_cost",
        "estimated_cost_saved",
        "circuit_open_count",
        "recovery_time_ms",
        "cache_false_hits",
    ]:
        lines.append(f"| {key} | {_fmt(metrics.get(key))} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]:
        lines.append(
            f"| {key} | {_fmt(without_cache.get(key))} | {_fmt(with_cache.get(key))} | "
            f"{_fmt(delta.get(key))} ({_pct_delta(delta.get(key), without_cache.get(key))}) |"
        )

    lines += [
        "",
        "The cache skips privacy-like queries and records false-hit candidates when similar prompts "
        "contain conflicting four-digit numbers such as different policy years.",
        "",
        "## 6. Redis shared cache",
        "",
        (
            "In-memory cache is per process, so horizontally scaled gateway instances would each "
            "warm their own cache and pay duplicate provider cost. SharedRedisCache stores query "
            "hashes, original queries, responses, metadata, and TTLs in Redis so separate instances "
            "can reuse the same safe cache entries."
        ),
        "",
        "### Evidence of shared state",
        "",
        "```text",
        f"Redis enabled: {_fmt(redis_evidence.get('enabled'))}",
        f"Redis available: {_fmt(redis_evidence.get('available'))}",
        f"Two cache instances read same entry: {_fmt(redis_evidence.get('shared_state_pass'))}",
        f"Similarity score: {_fmt(redis_evidence.get('shared_state_score'))}",
        "```",
        "",
        "### Redis CLI output",
        "",
        "```text",
        '$ docker compose exec redis redis-cli KEYS "rl:cache:*"',
    ]
    sample_keys = redis_evidence.get("sample_keys") or []
    if sample_keys:
        lines.extend(str(key) for key in sample_keys)
    else:
        lines.append("(no Redis keys captured; Redis may have been unavailable)")
    lines += [
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]

    expected = {
        "primary_timeout_100": "Primary fails; backup serves all provider traffic and circuit opens.",
        "primary_flaky_50": "Primary is unstable; circuit opens and traffic mixes primary/backup.",
        "all_healthy": "Primary serves cache misses; no circuit opens.",
        "cache_stale_candidate": "Different policy years are detected as cache false-hit candidates.",
    }
    for name, status in metrics.get("scenarios", {}).items():
        summary = scenario_metrics.get(name, {})
        lines.append(
            f"| {name} | {expected.get(name, 'Scenario-specific reliability behavior is exercised.')} | "
            f"{_scenario_observation(summary)} | {status} |"
        )

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        (
            "The remaining production weakness is that circuit breaker state is process-local. "
            "With many gateway replicas, one instance can learn that a provider is failing while "
            "another instance continues sending traffic until its own breaker opens. I would move "
            "breaker counters and open timestamps to Redis with atomic increments and expirations, "
            "or publish provider health through a small control plane."
        ),
        "",
        "## 9. Next steps",
        "",
        "1. Store circuit breaker state in Redis so provider health is shared across instances.",
        "2. Add per-user rate limits and cache namespaces to reduce privacy and tenant-isolation risk.",
        "3. Export Prometheus counters for requests, cache hits, fallback routes, latency, and circuit state.",
        "",
    ]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
