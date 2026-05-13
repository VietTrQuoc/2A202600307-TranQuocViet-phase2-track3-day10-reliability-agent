# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks a safety-aware cache first, then routes cache misses through per-provider circuit breakers. If the primary provider fails or its circuit is open, traffic moves to the backup provider; if every provider is unavailable, the gateway returns a static degraded-service response.

```text
User Request
    |
    v
[ReliabilityGateway] -> [Cache: Redis/shared or memory] -> hit: return cached
    | miss
    v
[CircuitBreaker: primary] -> Provider primary
    | open/error
    v
[CircuitBreaker: backup] -> Provider backup
    | open/error
    v
[Static fallback]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Detects repeated provider failure quickly while tolerating isolated jitter. |
| reset_timeout_seconds | 1.0 | Allows recovery probes during the short local chaos run. |
| success_threshold | 1 | One successful probe is enough for this simulated provider pool. |
| cache backend | redis | Redis demonstrates shared cache state; code falls back to memory when Redis is down. |
| cache TTL | 300 | Five minutes is long enough for FAQ reuse but short enough to limit stale answers. |
| similarity_threshold | 0.92 | High threshold favors exact or very close matches and avoids broad semantic false hits. |
| load_test requests | 120 | Enough volume to trigger circuit transitions and cache reuse. |
| load_test concurrency | 8 | Exercises the gateway under parallel requests without making the lab slow. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 1 | Yes |
| Latency P95 | < 2500 ms | 489.84 | Yes |
| Fallback success rate | >= 95% | 1 | Yes |
| Cache hit rate | >= 10% | 0.3875 | Yes |
| Recovery time | < 5000 ms | 1239.6452 | Yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 480 |
| availability | 1 |
| error_rate | 0 |
| latency_p50_ms | 211.71 |
| latency_p95_ms | 489.84 |
| latency_p99_ms | 516.97 |
| fallback_success_rate | 1 |
| cache_hit_rate | 0.3875 |
| estimated_cost | 0.132 |
| estimated_cost_saved | 0.1061 |
| circuit_open_count | 5 |
| recovery_time_ms | 1239.6452 |
| cache_false_hits | 1 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 208.27 | 0.36 | -207.91 (-99.8%) |
| latency_p95_ms | 237.12 | 231.19 | -5.93 (-2.5%) |
| estimated_cost | 0.0701 | 0.0184 | -0.0517 (-73.8%) |
| cache_hit_rate | 0 | 0.7417 | 0.7417 (0.7417) |

The cache skips privacy-like queries and records false-hit candidates when similar prompts contain conflicting four-digit numbers such as different policy years.

## 6. Redis shared cache

In-memory cache is per process, so horizontally scaled gateway instances would each warm their own cache and pay duplicate provider cost. SharedRedisCache stores query hashes, original queries, responses, metadata, and TTLs in Redis so separate instances can reuse the same safe cache entries.

### Evidence of shared state

```text
Redis enabled: True
Redis available: True
Two cache instances read same entry: True
Similarity score: 1
```

### Redis CLI output

```text
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:8baa2cfa11fa
rl:cache:9e413fd814eb
rl:cache:evidence:2c35d2f84b33
rl:cache:b2a52f7dc795
rl:cache:095946136fea
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails; backup serves all provider traffic and circuit opens. | availability=1, routes={'fallback': 120}, providers={'backup': 120}, circuit_open_count=4, recovery_time_ms=n/a | pass |
| primary_flaky_50 | Primary is unstable; circuit opens and traffic mixes primary/backup. | availability=1, routes={'primary': 57, 'fallback': 63}, providers={'primary': 57, 'backup': 63}, circuit_open_count=1, recovery_time_ms=1239.6452 | pass |
| all_healthy | Primary serves cache misses; no circuit opens. | availability=1, routes={'primary': 45, 'cache_hit': 75}, providers={'primary': 45}, circuit_open_count=0, recovery_time_ms=n/a | pass |
| cache_stale_candidate | Different policy years are detected as cache false-hit candidates. | availability=1, routes={'primary': 9, 'cache_hit': 111}, providers={'primary': 9}, circuit_open_count=0, recovery_time_ms=n/a | pass |

## 8. Failure analysis

The remaining production weakness is that circuit breaker state is process-local. With many gateway replicas, one instance can learn that a provider is failing while another instance continues sending traffic until its own breaker opens. I would move breaker counters and open timestamps to Redis with atomic increments and expirations, or publish provider health through a small control plane.

## 9. Next steps

1. Store circuit breaker state in Redis so provider health is shared across instances.
2. Add per-user rate limits and cache namespaces to reduce privacy and tenant-isolation risk.
3. Export Prometheus counters for requests, cache hits, fallback routes, latency, and circuit state.
