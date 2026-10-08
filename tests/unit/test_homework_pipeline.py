"""Exercise the real pipeline with deterministic external service substitutes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, SecurityConfig, ValidationConfig
from pg_mcp.models.errors import LLMTimeoutError, LLMUnavailableError
from pg_mcp.models.query import QueryRequest
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, request_context
from pg_mcp.resilience.rate_limiter import MultiRateLimiter, RateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_validator import SQLValidator


def pipeline(*, sql="SELECT 1", databases=("one",), resilience=None, validation=None, **kwargs):
    generator = AsyncMock()
    generator.generate.return_value = sql
    cache = MagicMock()
    cache.get.side_effect = lambda name: DatabaseSchema(database_name=name, tables=[], version="16")
    executors = {}
    for name in databases:
        executor = AsyncMock()
        executor.execute.return_value = ([{"database": name}], 1)
        executors[name] = executor
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validator=SQLValidator(SecurityConfig()),
        sql_executor=None,
        sql_executors=executors,
        result_validator=AsyncMock(),
        schema_cache=cache,
        pools=dict.fromkeys(databases, MagicMock()),
        resilience_config=resilience or ResilienceConfig(),
        validation_config=validation or ValidationConfig(enabled=False),
        **kwargs,
    )


async def test_secondary_database_uses_its_executor_and_schema():
    app = pipeline(databases=("one", "two"))
    response = await app.execute_query(QueryRequest(question="sentinel", database="two"))
    assert response.success
    assert response.data.rows == [{"database": "two"}]
    assert response.database == "two"
    assert app.sql_generator.generate.call_args.kwargs["schema"].database_name == "two"


@pytest.mark.parametrize("database", [None, "unknown"])
async def test_unresolved_database_never_calls_model(database):
    app = pipeline(databases=("one", "two"))
    response = await app.execute_query(QueryRequest(question="sentinel", database=database))
    assert not response.success
    assert app.sql_generator.generate.await_count == 0


async def test_security_rejection_is_not_retried():
    app = pipeline(sql="DELETE FROM users")
    response = await app.execute_query(QueryRequest(question="delete"))
    assert response.error.code == "security_violation"
    assert app.sql_generator.generate.await_count == 1


async def test_transient_retry_uses_capped_delay(monkeypatch):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", sleep)
    app = pipeline(resilience=ResilienceConfig(max_retries=3, retry_delay=1, max_retry_delay=2))
    app.sql_generator.generate.side_effect = [
        LLMTimeoutError("timeout"),
        LLMTimeoutError("timeout"),
        LLMTimeoutError("timeout"),
        "SELECT 1",
    ]
    response = await app.execute_query(QueryRequest(question="sentinel"))
    assert response.success
    assert delays == [1, 2, 2]


async def test_authentication_failure_not_retried():
    app = pipeline()
    app.sql_generator.generate.side_effect = LLMUnavailableError("authentication failed")
    response = await app.execute_query(QueryRequest(question="sentinel"))
    assert not response.success
    assert app.sql_generator.generate.await_count == 1


async def test_transient_retry_budget_exhaustion(monkeypatch):
    monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", AsyncMock())
    app = pipeline(resilience=ResilienceConfig(max_retries=2))
    app.sql_generator.generate.side_effect = LLMTimeoutError("timeout")
    response = await app.execute_query(QueryRequest(question="sentinel"))
    assert response.error.code == "llm_timeout"
    assert app.sql_generator.generate.await_count == 3


@pytest.mark.parametrize("resource", ["query_limiter", "llm_limiter"])
async def test_bounded_queue_returns_rate_limit_error(resource):
    limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
    held = getattr(limiter, resource)
    await held.acquire()
    app = pipeline(rate_limiter=limiter, resilience=ResilienceConfig(queue_timeout=0.01))
    try:
        response = await app.execute_query(QueryRequest(question="sentinel"))
        assert response.error.code == "rate_limit_exceeded"
        assert app.sql_generator.generate.await_count == 0
    finally:
        held.release()
    assert limiter.query_limiter.active_count == 0


async def test_cancellation_releases_active_slot_immediately():
    limiter = RateLimiter(1)
    entered = asyncio.Event()

    async def work():
        async with limiter():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.active_count == 0
    assert await limiter.acquire(timeout=0.01)
    limiter.release()


async def test_existing_context_and_concurrent_ids_are_isolated():
    app = pipeline()

    async def generate(**kwargs):
        current = get_request_id()
        await asyncio.sleep(0)
        assert get_request_id() == current
        return "SELECT 1"

    app.sql_generator.generate.side_effect = generate

    async def run(name):
        async with request_context(name):
            return await app.execute_query(QueryRequest(question="sentinel", return_type="sql"))

    responses = await asyncio.gather(run("first"), run("second"))
    assert [response.request_id for response in responses] == ["first", "second"]
    assert all(response.success for response in responses)
    assert get_request_id() is None


async def test_actual_metrics_include_failed_requests_and_security():
    metrics = MetricsCollector()
    before = metrics.sql_rejected.labels(reason="security_violation")._value.get()
    requests = metrics.query_requests.labels(status="error", database="one")
    count = requests._value.get()
    app = pipeline(sql="DELETE FROM users", metrics=metrics)
    response = await app.execute_query(QueryRequest(question="delete"))
    assert not response.success
    assert requests._value.get() == count + 1
    assert metrics.sql_rejected.labels(reason="security_violation")._value.get() == before + 1


async def test_question_length_config_prevents_external_calls():
    app = pipeline(validation=ValidationConfig(max_question_length=3, enabled=False))
    response = await app.execute_query(QueryRequest(question="too long"))
    assert response.error.code == "question_too_long"
    assert app.sql_generator.generate.await_count == 0


async def test_missing_executor_map_never_falls_back_to_primary():
    app = pipeline(databases=("one", "two"))
    app.sql_executors.clear()
    app.sql_executor = AsyncMock()
    response = await app.execute_query(QueryRequest(question="sentinel", database="two"))
    assert response.error.code == "database_error"
    assert app.sql_generator.generate.await_count == 0
    assert app.sql_executor.execute.await_count == 0


async def test_result_validation_queue_uses_shared_llm_limit():
    limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
    app = pipeline(
        rate_limiter=limiter,
        validation=ValidationConfig(enabled=True),
        resilience=ResilienceConfig(queue_timeout=0.01),
    )

    async def execute(sql):
        await limiter.llm_limiter.acquire()
        return [{"sentinel": 1}], 1

    app.sql_executors["one"].execute.side_effect = execute
    try:
        response = await app.execute_query(QueryRequest(question="sentinel"))
        assert response.error.code == "rate_limit_exceeded"
        assert app.result_validator.validate.await_count == 0
    finally:
        limiter.llm_limiter.release()
    assert limiter.query_limiter.active_count == 0


async def test_failed_response_keeps_measured_generation_token_usage():
    from pg_mcp.observability.tracing import record_llm_tokens

    app = pipeline(metrics=MetricsCollector())
    counter = app.metrics.llm_tokens_used.labels(operation="generate_sql")
    previous = counter._value.get()

    async def generate(**kwargs):
        record_llm_tokens(17)
        return "DELETE FROM users"

    app.sql_generator.generate.side_effect = generate
    response = await app.execute_query(QueryRequest(question="sentinel"))
    assert response.error.code == "security_violation"
    assert response.tokens_used == 17
    assert counter._value.get() == previous + 17
