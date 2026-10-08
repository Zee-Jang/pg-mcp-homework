"""Real PostgreSQL and MCP transport; only the external language model is simulated."""

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from pg_mcp import server
from pg_mcp.config.settings import Settings
from pg_mcp.services.sql_generator import SQLGenerator

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("HOMEWORK_PG_TESTS") != "1",
        reason="Start the homework PostgreSQL fixture and set HOMEWORK_PG_TESTS=1",
    ),
]


@pytest.fixture
async def mcp_client() -> AsyncIterator[Client]:
    """Run the actual server lifecycle with dedicated, non-superuser databases."""
    port = int(os.getenv("HOMEWORK_PG_PORT", "55439"))
    settings = Settings(
        _env_file=None,
        databases=[
            {
                "name": name,
                "host": "127.0.0.1",
                "port": port,
                "user": "homework_reader",
                "password": "homework-reader-demo",
                "min_pool_size": 1,
                "max_pool_size": 2,
            }
            for name in ("homework_sales", "homework_archive")
        ],
        openai={"api_key": "sk-homework-fake-never-sent"},
        security={
            "blocked_tables": ["public.audit_log"],
            "blocked_columns": ["users.password"],
            "allow_explain": True,
        },
        validation={"enabled": False},
        observability={"metrics_enabled": False, "log_level": "WARNING"},
        resilience={"max_retries": 0},
    )

    async def simulated_model(self: SQLGenerator, question: str, **kwargs: Any) -> str:
        # This fixture deliberately treats the test question as generated SQL.
        # Production retains its real natural-language generator.
        return question

    with (
        patch.object(server, "Settings", return_value=settings),
        patch.object(SQLGenerator, "generate", simulated_model),
    ):
        async with Client(server.mcp) as client:
            yield client


@pytest.mark.parametrize(
    ("database", "expected_total", "expected_count"),
    [("homework_sales", 400, 3), ("homework_archive", 1000, 2)],
)
async def test_mcp_routes_to_real_database(
    mcp_client: Client, database: str, expected_total: int, expected_count: int
) -> None:
    """Reusing the first executor would return the wrong total and database name."""
    result = await mcp_client.call_tool(
        "query",
        {
            "question": (
                "SELECT current_database() AS db, SUM(amount) AS total, COUNT(*) AS n FROM orders"
            ),
            "database": database,
        },
    )
    data = result.data
    assert data["success"] is True, data
    assert data["database"] == database
    assert data["data"]["rows"] == [{"db": database, "total": expected_total, "n": expected_count}]
    assert data["request_id"]
    assert data["tokens_used"] == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.password FROM public.users AS u",
        "SELECT u.* FROM public.users AS u",
        "SELECT * FROM (SELECT password FROM users) AS x",
        "SELECT (SELECT password FROM orders LIMIT 1) AS leaked FROM users",
        "SELECT * FROM public.audit_log",
        "EXPLAIN ANALYZE SELECT id FROM orders",
        "EXPLAIN DELETE FROM orders",
        "DELETE FROM orders",
    ],
)
async def test_mcp_blocks_policy_violations_before_database(mcp_client: Client, sql: str) -> None:
    """Policy errors must be security_violation, not a DB permission error."""
    result = await mcp_client.call_tool("query", {"question": sql, "database": "homework_sales"})
    assert result.data["success"] is False, result.data
    assert result.data["error"]["code"] == "security_violation"
    assert result.data["request_id"]


async def test_mcp_allows_safe_explain(mcp_client: Client) -> None:
    """EXPLAIN is executed on PostgreSQL only after validating its inner SELECT."""
    result = await mcp_client.call_tool(
        "query",
        {"question": "EXPLAIN SELECT id FROM orders", "database": "homework_sales"},
    )
    assert result.data["success"] is True, result.data
    assert "QUERY PLAN" in result.data["data"]["rows"][0]


async def test_mcp_requires_explicit_database_when_ambiguous(mcp_client: Client) -> None:
    """An omitted or unknown database cannot silently query the first pool."""
    for database in (None, "not_configured"):
        args = {"question": "SELECT 1"}
        if database is not None:
            args["database"] = database
        result = await mcp_client.call_tool("query", args)
        assert result.data["success"] is False, result.data
        assert result.data["error"]["code"] == "database_error"


async def test_database_role_also_blocks_sensitive_data() -> None:
    """The demo database grants enforce column protection independently of the app."""
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=int(os.getenv("HOMEWORK_PG_PORT", "55439")),
        database="homework_sales",
        user="homework_reader",
        password="homework-reader-demo",
    )
    try:
        assert await connection.fetchval("SELECT COUNT(*) FROM orders") == 3
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT password FROM users")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT detail FROM audit_log")
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await connection.execute("DELETE FROM orders")
    finally:
        await connection.close()


async def test_documented_stdio_entrypoint(tmp_path: Path) -> None:
    """The documented process serves PostgreSQL query tools with clean stdio."""
    project = Path(__file__).resolve().parents[2]
    databases = [
        {
            "name": name,
            "host": "127.0.0.1",
            "port": int(os.getenv("HOMEWORK_PG_PORT", "55439")),
            "user": "homework_reader",
            "password": "homework-reader-demo",
            "min_pool_size": 1,
            "max_pool_size": 2,
        }
        for name in ("homework_sales", "homework_archive")
    ]
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "pg_mcp"],
        cwd=str(project),
        env={
            "DATABASES": json.dumps(databases),
            "OPENAI_API_KEY": "sk-homework-fake-never-sent",
            "VALIDATION_ENABLED": "false",
            "OBSERVABILITY_METRICS_ENABLED": "false",
            "OBSERVABILITY_LOG_FORMAT": "json",
            "OBSERVABILITY_LOG_LEVEL": "INFO",
        },
        log_file=tmp_path / "server-stderr.txt",
    )
    async with Client(transport) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert "query" in tools
        assert "add" not in tools
        # Rejection happens before any external model call.
        result = await client.call_tool(
            "query", {"question": "SELECT 1", "database": "not_configured"}
        )
        assert result.data["success"] is False
        assert result.data["error"]["code"] == "database_error"
        assert result.data["request_id"]
