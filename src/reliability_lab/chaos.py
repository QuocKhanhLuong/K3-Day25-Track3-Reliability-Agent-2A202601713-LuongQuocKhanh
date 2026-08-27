from __future__ import annotations

import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    """Load query strings from the JSONL fixture."""
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        query = payload.get("query")
        if isinstance(query, str):
            queries.append(query)
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
) -> ReliabilityGateway:
    """Build a fresh gateway for one isolated chaos scenario."""
    providers = []
    for provider_config in config.providers:
        fail_rate = (
            provider_overrides.get(provider_config.name, provider_config.fail_rate)
            if provider_overrides
            else provider_config.fail_rate
        )
        providers.append(
            FakeLLMProvider(
                provider_config.name,
                fail_rate,
                provider_config.base_latency_ms,
                provider_config.cost_per_1k_tokens,
            )
        )

    breakers = {
        provider_config.name: CircuitBreaker(
            name=provider_config.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for provider_config in config.providers
    }

    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Average elapsed time from OPEN to the next CLOSED transition."""
    recovery_times_ms: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            state_to = transition.get("to")
            timestamp = transition.get("ts")
            if not isinstance(timestamp, (int, float)):
                continue
            if state_to == "open":
                opened_at = float(timestamp)
            elif state_to == "closed" and opened_at is not None:
                recovery_times_ms.append((float(timestamp) - opened_at) * 1000.0)
                opened_at = None

    if not recovery_times_ms:
        return None
    return sum(recovery_times_ms) / len(recovery_times_ms)


def _scenario_seed(base_seed: int, name: str) -> int:
    return base_seed + sum((index + 1) * ord(char) for index, char in enumerate(name))


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Execute one deterministic chaos scenario and collect reliability metrics."""
    random.seed(_scenario_seed(config.seed, scenario.name))
    gateway = build_gateway(config, scenario.provider_overrides or None)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.flush()

    metrics = RunMetrics()
    for _ in range(config.load_test.requests):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)

        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1

        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition.get("to") == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)

    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return metrics.fallback_success_rate >= 0.90 and metrics.circuit_open_count > 0
    if name == "primary_flaky_50":
        return metrics.availability >= 0.95 and metrics.circuit_open_count > 0
    if name == "all_healthy":
        return metrics.availability >= 0.99 and metrics.circuit_open_count == 0
    return metrics.availability >= 0.95


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all configured scenarios and aggregate their metrics."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        status = "pass" if metrics.successful_requests > 0 else "fail"
        metrics.scenarios = {"default": status}
        return metrics

    combined = RunMetrics()
    recovery_times_ms: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = (
            "pass" if _scenario_passed(scenario.name, result) else "fail"
        )
        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            recovery_times_ms.append(result.recovery_time_ms)

    if recovery_times_ms:
        combined.recovery_time_ms = sum(recovery_times_ms) / len(recovery_times_ms)
    return combined
