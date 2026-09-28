import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# For HTTP calls to external microservices (Rival, GPTCache).
#
# ReadTimeout and HTTPStatusError are worth retrying: the service answered, or
# was reachable and slow, so a second attempt may succeed.
#
# ConnectTimeout and ConnectError are NOT. If we cannot open a socket, the host
# is absent or unreachable, and three attempts with backoff just add ~18s to
# every request before the same failure. Measured: that was 18 of 150 seconds on
# a deployment where rival-service is intentionally not running. The circuit
# breaker is what handles repeated failure; retries are for transient ones.
#
# Note httpx.ConnectTimeout subclasses TimeoutException, so listing
# TimeoutException would silently re-include it.
http_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                                   httpx.HTTPStatusError)),
    reraise=True,
)

# For LLM API calls (rate limits, transient errors)
llm_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
