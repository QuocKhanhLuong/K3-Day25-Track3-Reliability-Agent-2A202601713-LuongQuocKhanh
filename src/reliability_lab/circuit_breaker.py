from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Three-state circuit breaker for provider calls."""

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted in the current state."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.HALF_OPEN:
            return True
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_timeout_seconds:
            self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
            return True
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call ``fn`` through the breaker and record the outcome."""
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit {self.name!r} is open")

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise

        self.record_success()
        return result

    def record_success(self) -> None:
        """Record a provider success and close a successful HALF_OPEN probe."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0
            self.opened_at = None

    def record_failure(self) -> None:
        """Record a provider failure and open/re-open the circuit when required."""
        self.failure_count += 1
        self.success_count = 0
        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self.opened_at = time.monotonic()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {
                "from": self.state.value,
                "to": new_state.value,
                "reason": reason,
                "ts": time.time(),
            }
        )
        self.state = new_state
