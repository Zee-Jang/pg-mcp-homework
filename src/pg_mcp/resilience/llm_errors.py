"""Classify provider failures without exposing provider bodies or credentials."""

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError

from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError


def classify_llm_error(error: Exception) -> LLMError:
    if isinstance(error, (APITimeoutError, TimeoutError)):
        return LLMTimeoutError("OpenAI API request timed out", {"retryable": True})
    if isinstance(error, AuthenticationError):
        return LLMUnavailableError("OpenAI API authentication failed", {"retryable": False})
    if isinstance(error, APIStatusError):
        retryable = error.status_code == 429 or error.status_code >= 500
        return LLMUnavailableError(
            "OpenAI API request rejected",
            {"status_code": error.status_code, "retryable": retryable},
        )
    if isinstance(error, (APIConnectionError, ConnectionError)):
        return LLMUnavailableError("OpenAI API connection failed", {"retryable": True})
    # Compatibility for older clients that raise generic exceptions.
    message = str(error).lower()
    if "authentication" in message or "api_key" in message:
        return LLMUnavailableError("OpenAI API authentication failed", {"retryable": False})
    if "rate_limit" in message:
        return LLMUnavailableError("OpenAI API rate limit exceeded", {"retryable": True})
    return LLMError("OpenAI API request failed", details={"error_type": type(error).__name__})
