from __future__ import annotations

import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }

    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            redis_cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
            if redis_cache.ping():
                cache = redis_cache
            else:
                redis_cache.close()
                cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _scenario_config(config: LabConfig, scenario: ScenarioConfig) -> LabConfig:
    scenario_config = config.model_copy(deep=True)
    if scenario.cache_enabled is not None:
        scenario_config.cache.enabled = scenario.cache_enabled
    if scenario.cache_backend is not None:
        scenario_config.cache.backend = scenario.cache_backend
    if scenario.cache_similarity_threshold is not None:
        scenario_config.cache.similarity_threshold = scenario.cache_similarity_threshold
    return scenario_config


def _stable_seed(name: str) -> int:
    return int(hashlib.md5(name.encode()).hexdigest()[:8], 16)


def _prompts_for_scenario(scenario: ScenarioConfig, queries: list[str], request_count: int) -> list[str]:
    random.seed(_stable_seed(scenario.name))
    if scenario.name == "cache_stale_candidate":
        stale_queries = [
            "Summarize refund policy for 2024 deadline",
            "Summarize refund policy for 2026 deadline",
        ]
        return [stale_queries[index % len(stale_queries)] for index in range(request_count)]
    return [random.choice(queries) for _ in range(request_count)]


def _estimate_cache_saved(prompt: str, config: LabConfig) -> float:
    if not config.providers:
        return 0.0
    average_output_tokens = 50
    provider = config.providers[0]
    token_count = max(1, len(prompt.split())) + average_output_tokens
    return token_count / 1000.0 * provider.cost_per_1k_tokens


def _record_result(
    metrics: RunMetrics,
    result: GatewayResponse,
    prompt: str,
    config: LabConfig,
) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost
    metrics.latencies_ms.append(result.latency_ms)
    metrics.route_counts[result.route] = metrics.route_counts.get(result.route, 0) + 1
    if result.provider is not None:
        metrics.provider_counts[result.provider] = metrics.provider_counts.get(result.provider, 0) + 1

    if result.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += _estimate_cache_saved(prompt, config)

    if result.route == "fallback":
        metrics.fallback_successes += 1
    elif result.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
        return

    metrics.successful_requests += 1


def _flush_cache(gateway: ReliabilityGateway) -> None:
    cache = gateway.cache
    if isinstance(cache, SharedRedisCache):
        cache.flush()


def _close_gateway(gateway: ReliabilityGateway) -> None:
    cache = gateway.cache
    if isinstance(cache, SharedRedisCache):
        cache.close()


def _cache_false_hits(gateway: ReliabilityGateway) -> int:
    cache = gateway.cache
    if cache is None:
        return 0
    return len(cache.false_hit_log)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    scenario_config = _scenario_config(config, scenario)
    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)
    _flush_cache(gateway)

    metrics = RunMetrics()
    request_count = scenario_config.load_test.requests
    prompts = _prompts_for_scenario(scenario, queries, request_count)
    concurrency = min(scenario_config.load_test.concurrency, request_count)

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(gateway.complete, prompts))
    else:
        results = [gateway.complete(prompt) for prompt in prompts]

    for prompt, result in zip(prompts, results):
        _record_result(metrics, result, prompt, scenario_config)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for entry in breaker.transition_log if entry["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    metrics.cache_false_hits = _cache_false_hits(gateway)
    _close_gateway(gateway)
    return metrics


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _add_metrics(target: RunMetrics, source: RunMetrics) -> None:
    target.total_requests += source.total_requests
    target.successful_requests += source.successful_requests
    target.failed_requests += source.failed_requests
    target.fallback_successes += source.fallback_successes
    target.static_fallbacks += source.static_fallbacks
    target.cache_hits += source.cache_hits
    target.cache_false_hits += source.cache_false_hits
    target.circuit_open_count += source.circuit_open_count
    target.estimated_cost += source.estimated_cost
    target.estimated_cost_saved += source.estimated_cost_saved
    target.latencies_ms.extend(source.latencies_ms)
    _merge_counts(target.route_counts, source.route_counts)
    _merge_counts(target.provider_counts, source.provider_counts)
    if source.recovery_time_ms is not None:
        if target.recovery_time_ms is None:
            target.recovery_time_ms = source.recovery_time_ms
        else:
            target.recovery_time_ms = (target.recovery_time_ms + source.recovery_time_ms) / 2


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return (
            metrics.circuit_open_count > 0
            and metrics.fallback_success_rate >= 0.95
            and metrics.static_fallbacks == 0
        )
    if name == "primary_flaky_50":
        return (
            metrics.circuit_open_count > 0
            and metrics.route_counts.get("fallback", 0) > 0
            and metrics.successful_requests > 0
        )
    if name == "all_healthy":
        return metrics.availability >= 0.99 and metrics.circuit_open_count == 0
    if name == "cache_stale_candidate":
        return metrics.cache_false_hits > 0 and metrics.availability >= 0.99
    return metrics.successful_requests > 0


def _scenario_summary(metrics: RunMetrics) -> dict[str, object]:
    return {
        "total_requests": metrics.total_requests,
        "availability": round(metrics.availability, 4),
        "fallback_success_rate": round(metrics.fallback_success_rate, 4),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "cache_false_hits": metrics.cache_false_hits,
        "circuit_open_count": metrics.circuit_open_count,
        "recovery_time_ms": metrics.recovery_time_ms,
        "route_counts": metrics.route_counts,
        "provider_counts": metrics.provider_counts,
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "estimated_cost_saved": round(metrics.estimated_cost_saved, 6),
    }


def _comparison_summary(metrics: RunMetrics) -> dict[str, object]:
    return {
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "estimated_cost_saved": round(metrics.estimated_cost_saved, 6),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
    }


def _as_float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"Expected numeric value, got {type(value).__name__}")


def _delta(with_cache: dict[str, object], without_cache: dict[str, object]) -> dict[str, object]:
    delta: dict[str, object] = {}
    for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]:
        with_value = _as_float(with_cache[key])
        without_value = _as_float(without_cache[key])
        delta[key] = round(with_value - without_value, 6)
    return delta


def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    healthy_overrides = {provider.name: 0.0 for provider in config.providers}
    no_cache = ScenarioConfig(
        name="cache_comparison_without_cache",
        description="Healthy providers without cache",
        provider_overrides=healthy_overrides,
        cache_enabled=False,
    )
    with_cache = ScenarioConfig(
        name="cache_comparison_with_cache",
        description="Healthy providers with cache",
        provider_overrides=healthy_overrides,
        cache_enabled=True,
    )
    without_metrics = run_scenario(config, queries, no_cache)
    with_metrics = run_scenario(config, queries, with_cache)
    without_summary = _comparison_summary(without_metrics)
    with_summary = _comparison_summary(with_metrics)
    return {
        "without_cache": without_summary,
        "with_cache": with_summary,
        "delta": _delta(with_summary, without_summary),
    }


def collect_redis_evidence(config: LabConfig) -> dict[str, object]:
    if config.cache.backend != "redis":
        return {"enabled": False, "available": False, "note": "Config uses in-memory cache"}

    cache1 = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        prefix="rl:cache:evidence:",
    )
    cache2 = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        prefix="rl:cache:evidence:",
    )
    try:
        if not cache1.ping():
            return {"enabled": True, "available": False, "note": "Redis ping failed"}
        cache1.flush()
        cache1.set("shared evidence query", "shared evidence response")
        cached, score = cache2.get("shared evidence query")
        keys = list(cache1._redis.scan_iter("rl:cache:*"))
        return {
            "enabled": True,
            "available": True,
            "shared_state_pass": cached == "shared evidence response",
            "shared_state_score": score,
            "sample_keys": keys[:10],
        }
    finally:
        cache1.close()
        cache2.close()


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none are defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        metrics.scenario_metrics["default"] = _scenario_summary(metrics)
        metrics.cache_comparison = run_cache_comparison(config, queries)
        metrics.redis_evidence = collect_redis_evidence(config)
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = "pass" if _scenario_passed(scenario.name, result) else "fail"
        combined.scenario_metrics[scenario.name] = _scenario_summary(result)
        _add_metrics(combined, result)

    combined.cache_comparison = run_cache_comparison(config, queries)
    combined.redis_evidence = collect_redis_evidence(config)
    return combined
