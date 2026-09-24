import pybreaker
import structlog

log = structlog.get_logger()


class _LoggingListener(pybreaker.CircuitBreakerListener):
    """Emits a structured log line every time a breaker changes state.

    Without this, an open breaker is invisible — the calling node catches
    CircuitBreakerError and returns its safe default, so the only evidence
    that a dependency died is the absence of its normal log lines.
    """

    def state_change(self, cb, old_state, new_state):
        event = {
            pybreaker.STATE_OPEN: "circuit_breaker_opened",
            pybreaker.STATE_CLOSED: "circuit_breaker_closed",
            pybreaker.STATE_HALF_OPEN: "circuit_breaker_half_open",
        }.get(new_state.name, "circuit_breaker_state_change")

        level = log.warning if new_state.name == pybreaker.STATE_OPEN else log.info
        level(
            event,
            breaker=cb.name,
            old_state=old_state.name if old_state else None,
            new_state=new_state.name,
            fail_counter=cb.fail_counter,
        )

    def failure(self, cb, exc):
        log.warning(
            "circuit_breaker_failure",
            breaker=cb.name,
            error=str(exc),
            fail_counter=cb.fail_counter,
            fail_max=cb.fail_max,
        )


# NOTE: breakers are module-level singletons on purpose — failure counts must be
# shared across every request in the process. With uvicorn --workers N, each
# worker holds its own independent breaker state.

rival_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=30,
    name="rival",
    listeners=[_LoggingListener()],
)

pageindex_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=30,
    name="pageindex",
    listeners=[_LoggingListener()],
)

gptcache_breaker = pybreaker.CircuitBreaker(
    fail_max=10,
    reset_timeout=15,
    name="gptcache",
    listeners=[_LoggingListener()],
)
