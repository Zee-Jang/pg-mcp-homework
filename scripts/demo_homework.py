"""Reproducible checks for PostgreSQL routing, policy enforcement, and recovery.

Run after starting the test database. The output records tool responses and
fails if any expected behavior fails.
"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncpg
from fastmcp import Client
from prometheus_client import REGISTRY

from pg_mcp import server
from pg_mcp.config.settings import Settings
from pg_mcp.models.errors import LLMTimeoutError
from pg_mcp.observability.tracing import get_request_id
from pg_mcp.services.sql_generator import SQLGenerator

TOTAL_SQL = "SELECT current_database() AS db, SUM(amount) AS total, COUNT(*) AS n FROM orders"
SQL_BY_QUESTION = {
    "统计订单总额": TOTAL_SQL,
    "查询用户姓名": "SELECT id, name FROM users ORDER BY id",
    "查询用户密码": "SELECT u.password FROM public.users AS u",
    "查询用户全部字段": "SELECT u.* FROM public.users AS u",
    "读取审计记录": "SELECT * FROM public.audit_log",
    "查看订单查询计划": "EXPLAIN SELECT id FROM orders",
    "执行分析查询计划": "EXPLAIN ANALYZE SELECT id FROM orders",
    "模拟临时超时后重试": "SELECT COUNT(*) AS n FROM orders",
    "占用一个并发槽": "SELECT COUNT(*) AS n FROM orders",
}


def metric_totals() -> dict[str, float]:
    """Read actual Prometheus counters and observation counts, across labels."""
    wanted = {
        "pg_mcp_query_requests_total",
        "pg_mcp_llm_calls_total",
        "pg_mcp_sql_rejected_total",
        "pg_mcp_query_duration_seconds_count",
        "pg_mcp_db_query_duration_seconds_count",
    }
    values = dict.fromkeys(wanted, 0.0)
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name in wanted:
                values[sample.name] += sample.value
    return values


async def run_demo(port: int, output: Path) -> dict[str, Any]:
    """Execute and save assertions against a dedicated read-only database fixture."""
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=port,
        database="homework_sales",
        user="homework_reader",
        password="homework-reader-demo",  # noqa: S106 - public localhost-only fixture
        timeout=5,
    )
    try:
        version = await connection.fetchval("SELECT version()")
    finally:
        await connection.close()

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
        observability={"metrics_enabled": True, "metrics_port": 19139, "log_level": "INFO"},
        resilience={
            "query_concurrency": 1,
            "llm_concurrency": 1,
            "queue_timeout": 0.05,
            "max_retries": 2,
            "retry_delay": 0.1,
        },
    )
    calls: list[dict[str, str | None]] = []
    transient_attempts = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def simulated_model(self: SQLGenerator, question: str, **kwargs: Any) -> str:
        nonlocal transient_attempts
        calls.append({"question": question, "request_id": get_request_id()})
        if question == "模拟临时超时后重试":
            transient_attempts += 1
            if transient_attempts == 1:
                raise LLMTimeoutError("Deliberate demo timeout; no network call made")
        if question == "占用一个并发槽":
            entered.set()
            await release.wait()
        return SQL_BY_QUESTION[question]

    cases: list[dict[str, Any]] = []

    async def run_case(
        client: Client,
        name: str,
        question: str,
        database: str | None = "homework_sales",
        *,
        code: str | None = None,
        rows: list[dict[str, Any]] | None = None,
        return_type: str = "result",
    ) -> dict[str, Any]:
        args = {"question": question, "return_type": return_type}
        if database is not None:
            args["database"] = database
        before = len(calls)
        result = await client.call_tool("query", args, timeout=15)
        response = result.data
        passed = response.get("success") is (code is None)
        if code is not None:
            passed = passed and response.get("error", {}).get("code") == code
        if rows is not None:
            passed = passed and response.get("data", {}).get("rows") == rows
        passed = passed and bool(response.get("request_id"))
        if name == "临时故障退避重试":
            passed = passed and len(calls) - before == 2
        if name in ("未知数据库", "未指定数据库"):
            passed = passed and len(calls) == before
        if code == "security_violation":
            passed = passed and len(calls) - before == 1
        if return_type == "sql":
            passed = passed and not response.get("data")
        case = {
            "name": name,
            "passed": bool(passed),
            "input": args,
            "expected_code": code,
            "model_calls": len(calls) - before,
            "response": response,
        }
        cases.append(case)
        print(f"{'PASS' if passed else 'FAIL'} | {name}", flush=True)
        return case

    before_metrics = metric_totals()
    with (
        patch.object(server, "Settings", return_value=settings),
        patch.object(SQLGenerator, "generate", simulated_model),
    ):
        async with Client(server.mcp) as client:
            await run_case(
                client,
                "销售库路由",
                "统计订单总额",
                rows=[{"db": "homework_sales", "total": 400, "n": 3}],
            )
            await run_case(
                client,
                "归档库路由",
                "统计订单总额",
                "homework_archive",
                rows=[{"db": "homework_archive", "total": 1000, "n": 2}],
            )
            await run_case(
                client, "普通字段查询", "查询用户姓名", rows=[{"id": 1, "name": "Alice"}]
            )
            for name, question in (
                ("敏感列别名拦截", "查询用户密码"),
                ("通配符拦截", "查询用户全部字段"),
                ("敏感表拦截", "读取审计记录"),
                ("EXPLAIN ANALYZE 拦截", "执行分析查询计划"),
            ):
                await run_case(client, name, question, code="security_violation")
            explain = await run_case(client, "安全 EXPLAIN", "查看订单查询计划")
            explain["passed"] = (
                explain["passed"]
                and "QUERY PLAN" in (explain["response"].get("data", {}).get("rows", [{}])[0])
            )
            await run_case(
                client, "未知数据库", "统计订单总额", "not_configured", code="database_error"
            )
            await run_case(client, "未指定数据库", "统计订单总额", None, code="database_error")
            await run_case(client, "临时故障退避重试", "模拟临时超时后重试", rows=[{"n": 3}])
            await run_case(client, "仅返回 SQL", "统计订单总额", return_type="sql")
            holding = asyncio.create_task(
                run_case(client, "并发槽释放", "占用一个并发槽", rows=[{"n": 3}])
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                await run_case(client, "超限请求拒绝", "统计订单总额", code="rate_limit_exceeded")
            finally:
                release.set()
                await holding
            await run_case(
                client,
                "限流后恢复查询",
                "统计订单总额",
                rows=[{"db": "homework_sales", "total": 400, "n": 3}],
            )
    after_metrics = metric_totals()
    request_ids = [case["response"].get("request_id") for case in cases]
    observed_ids = {call["request_id"] for call in calls}
    delta = {name: after_metrics[name] - value for name, value in before_metrics.items()}
    tracing_ok = len(set(request_ids)) == len(cases) and observed_ids.issubset(set(request_ids))
    metrics_ok = delta["pg_mcp_query_requests_total"] == len(cases)
    report = {
        "title": "PG MCP 第二次作业运行证据",
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "mode": (
            "真实 PostgreSQL + 真实 MCP 内存传输; 外部 LLM 使用固定输出测试替身, 未调用付费 API"
        ),
        "postgres_version": version,
        "cases": cases,
        "metrics_delta": delta,
        "request_tracing_passed": tracing_ok,
        "metrics_passed": metrics_ok,
        "all_passed": all(case["passed"] for case in cases) and tracing_ok and metrics_ok,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Evidence saved: {output}", flush=True)
    print(f"Metrics: {json.dumps(delta, ensure_ascii=False)}", flush=True)
    return report


def main() -> None:
    """Run the dedicated fixture demonstration and report nonzero on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.getenv("HOMEWORK_PG_PORT", "55439")))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs/evidence/demo.json",
    )
    args = parser.parse_args()
    report = asyncio.run(run_demo(args.port, args.output))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
