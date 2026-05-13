from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None
    route_reason: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        started_at = time.perf_counter()

        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                latency_ms = (time.perf_counter() - started_at) * 1000
                return GatewayResponse(
                    text=cached,
                    route="cache_hit",
                    provider=None,
                    cache_hit=True,
                    latency_ms=latency_ms,
                    estimated_cost=0.0,
                    route_reason=f"cache_hit:{score:.2f}",
                )

        last_error: str | None = None
        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                route = "primary" if index == 0 else "fallback"
                reason_prefix = "primary" if index == 0 else "fallback"
                latency_ms = (time.perf_counter() - started_at) * 1000
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=latency_ms,
                    estimated_cost=response.estimated_cost,
                    route_reason=f"{reason_prefix}:{provider.name}:circuit_{breaker.state.value}",
                )
            except CircuitOpenError as exc:
                last_error = f"{provider.name}:circuit_open:{exc}"
                continue
            except ProviderError as exc:
                last_error = f"{provider.name}:provider_error:{exc}"
                continue
            except Exception as exc:
                last_error = f"{provider.name}:unexpected_error:{exc}"
                continue

        latency_ms = (time.perf_counter() - started_at) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error=last_error,
            route_reason="static_fallback:all_providers_unavailable",
        )
