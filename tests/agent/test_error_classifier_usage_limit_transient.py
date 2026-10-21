"""Usage-limit 429s that name a reset time are transient, not billing.

A quota wall as 429 classifies as billing ONLY when the response carries no
reset signal (#93419, #39441). The zai/GLM coding plan's 429 code 1308 says
``Usage limit reached for 5 hour. Your limit will reset at <timestamp>``: the
message-level detector's ``resets at`` pattern missed the bare ``reset at``
phrasing, the body carries no ``resets_at``/``retry_after`` field and no
``Retry-After`` header arrives, so a response naming its own reset time was
classified billing: non-retryable, and the turn aborted on attempt 1 before
the failover dispatch could rotate onto the chain. These tests pin the
phrasing both ways.
"""
import httpx
import openai

from agent.error_classifier import FailoverReason, classify_api_error

_ZAI_5H = "Usage limit reached for 5 hour. Your limit will reset at 2026-10-21 21:00:52"


def _rate_limit(message: str, body: dict) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.z.ai/api/coding/paas/v4/chat/completions")
    return openai.RateLimitError(
        f"Error code: 429 - {{'error': {{'code': '1308', 'message': '{message}'}}}}",
        response=httpx.Response(429, request=request), body=body)


def test_usage_limit_that_names_its_reset_time_is_rate_limit():
    err = _rate_limit(_ZAI_5H, {"error": {"code": "1308", "message": _ZAI_5H}})
    result = classify_api_error(err, provider="zai", model="glm-5.3-flash")
    assert result.reason == FailoverReason.rate_limit
    assert result.should_fallback is True


def test_usage_limit_without_any_reset_signal_stays_billing():
    bare = "Usage limit reached for this account. Top up to continue."
    err = _rate_limit(bare, {"error": {"code": "1308", "message": bare}})
    result = classify_api_error(err, provider="zai", model="glm-5.3-flash")
    assert result.reason == FailoverReason.billing
    assert result.retryable is False
