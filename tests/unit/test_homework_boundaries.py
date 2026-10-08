"""Check actual server boundaries and typed OpenAI error behavior."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError, InternalServerError, RateLimitError

from pg_mcp import server
from pg_mcp.config.settings import DatabaseConfig, OpenAIConfig, SecurityConfig, Settings
from pg_mcp.models.errors import LLMError
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.observability.tracing import get_request_id, request_context, trace_async
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator


@pytest.mark.parametrize(
    "kind,retryable",
    [
        (APIConnectionError, True),
        (RateLimitError, True),
        (InternalServerError, True),
        (AuthenticationError, False),
    ],
)
async def test_real_openai_error_types_are_classified_without_body_leak(kind, retryable):
    generator = SQLGenerator(OpenAIConfig(api_key="sk-test"))
    request = httpx.Request("POST", "https://example.test")
    if kind is APIConnectionError:
        error = kind(request=request)
    else:
        error = kind(
            "private literal sk-secret",
            response=httpx.Response(500, request=request),
            body={"secret": "private literal"},
        )
    generator.client.chat.completions.create = AsyncMock(side_effect=error)
    with pytest.raises(LLMError) as raised:
        await generator.generate("sentinel", DatabaseSchema(database_name="test", version="16"))
    assert raised.value.details.get("retryable", False) is retryable
    assert "private literal" not in str(raised.value)
    assert "private literal" not in str(raised.value.details)
    assert generator.client.max_retries == 0


async def test_generation_token_usage_propagates_to_pipeline_metrics():
    from pg_mcp.models.query import QueryRequest
    from pg_mcp.observability.metrics import MetricsCollector
    from tests.unit.test_homework_pipeline import pipeline

    generator = SQLGenerator(OpenAIConfig(api_key="sk-test"))
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="SELECT 1"))]
    response.usage.total_tokens = 17
    generator.client.chat.completions.create = AsyncMock(return_value=response)
    metrics = MetricsCollector()
    counter = metrics.llm_tokens_used.labels(operation="generate_sql")
    previous = counter._value.get()
    app = pipeline(metrics=metrics)
    app.sql_generator = generator
    result = await app.execute_query(QueryRequest(question="sentinel", return_type="sql"))
    assert result.tokens_used == 17
    assert counter._value.get() == previous + 17


async def test_server_lifespan_propagates_all_databases_and_policy(monkeypatch):
    settings = Settings(
        openai=OpenAIConfig(api_key="sk-test"),
        databases=[
            DatabaseConfig(name="one"),
            DatabaseConfig(name="two", command_timeout=40),
        ],
        security=SecurityConfig(blocked_tables=["secrets"]),
    )
    monkeypatch.setattr(server, "Settings", lambda: settings)
    monkeypatch.setattr(server, "create_pool", AsyncMock(side_effect=[MagicMock(), MagicMock()]))
    monkeypatch.setattr(server, "close_pools", AsyncMock())
    monkeypatch.setattr(
        server.SchemaCache,
        "load",
        AsyncMock(return_value=DatabaseSchema(database_name="one", version="16")),
    )
    async with server.lifespan(server.mcp):
        app = server._orchestrator
        assert set(app.sql_executors) == {"one", "two"}
        assert app.sql_executors["two"].db_config.command_timeout == 40
        assert not app.sql_validator.validate("SELECT * FROM secrets")[0]
        assert app.metrics is None
        assert app.rate_limiter is server._rate_limiter
    assert server._orchestrator is None


async def test_early_tool_error_has_stable_envelope(monkeypatch):
    monkeypatch.setattr(server, "_orchestrator", None)
    result = await server.query("sentinel")
    assert result["tokens_used"] == 0
    assert result["request_id"]


async def test_trace_decorator_does_not_mutate_global_log_factory():
    factory = logging.getLogRecordFactory()

    @trace_async("stage")
    async def stage():
        await asyncio.sleep(0)
        logging.getLogger(__name__).info("stage", extra={"request_id": get_request_id()})
        assert logging.getLogRecordFactory() is factory

    async with request_context("outer"):
        await stage()


async def test_session_settings_are_local_to_readonly_transaction():
    executor = SQLExecutor(MagicMock(), SecurityConfig(readonly_role="reader"), DatabaseConfig())
    connection = AsyncMock()
    await executor._set_session_params(connection, 3)
    commands = [call.args[0] for call in connection.execute.call_args_list]
    assert all(command.startswith("SET LOCAL ") for command in commands)


def test_diagnostics_use_stderr(capsys):
    from pg_mcp.observability.logging import configure_logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging()
        logging.getLogger(__name__).info("diagnostic sentinel")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "diagnostic sentinel" in captured.err
    finally:
        root.handlers = handlers
        root.setLevel(level)


def test_opaque_parser_command_never_logs_sql(capsys):
    from pg_mcp.observability.logging import configure_logging
    from pg_mcp.services.sql_validator import SQLValidator

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging()
        assert not SQLValidator(SecurityConfig()).validate("VACUUM private_literal")[0]
        captured = capsys.readouterr()
        assert "private_literal" not in captured.err + captured.out
    finally:
        root.handlers = handlers
        root.setLevel(level)


async def test_invalid_model_output_never_echoes_provider_content():
    generator = SQLGenerator(OpenAIConfig(api_key="sk-test"))
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="sk-provider-secret"))]
    generator.client.chat.completions.create = AsyncMock(return_value=response)
    with pytest.raises(LLMError) as raised:
        await generator.generate("sentinel", DatabaseSchema(database_name="test", version="16"))
    assert "sk-provider-secret" not in str(raised.value.details)


async def test_metrics_http_server_is_stopped_during_shutdown(monkeypatch):
    settings = Settings(openai=OpenAIConfig(api_key="sk-test"))
    settings.observability.metrics_enabled = True
    monkeypatch.setattr(server, "Settings", lambda: settings)
    monkeypatch.setattr(server, "create_pool", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(server, "close_pools", AsyncMock())
    monkeypatch.setattr(
        server.SchemaCache,
        "load",
        AsyncMock(return_value=DatabaseSchema(database_name="test", version="16")),
    )
    http_server, thread = MagicMock(), MagicMock()
    monkeypatch.setattr("prometheus_client.start_http_server", lambda port: (http_server, thread))
    async with server.lifespan(server.mcp):
        assert server._orchestrator.metrics is not None
    http_server.shutdown.assert_called_once()
    http_server.server_close.assert_called_once()
