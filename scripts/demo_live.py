"""Exercise the stdio server with a configured model and the demo databases."""

# Natural-language prompts use Chinese punctuation.
# ruff: noqa: RUF001

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from dotenv import dotenv_values
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from prometheus_client.parser import text_string_to_metric_families

PROJECT = Path(__file__).resolve().parents[1]
TOTAL_QUESTION = (
    "统计 orders 表的订单数量和总金额，结果列名分别为 order_count 和 total_amount。"
)
CASES = [
    ("销售库统计", "homework_sales", TOTAL_QUESTION, [{"order_count": 3, "total_amount": 400}]),
    ("归档库统计", "homework_archive", TOTAL_QUESTION, [{"order_count": 2, "total_amount": 1000}]),
    (
        "普通字段查询",
        "homework_sales",
        "查询 users 表的 id 和 name，按 id 排序。",
        None,
    ),
    (
        "密码字段拦截",
        "homework_sales",
        "查询 public.users 表的 password 列，返回原始列名。",
        "security_violation",
    ),
]


async def read_metrics(port: int) -> dict[str, float]:
    wanted = {
        "pg_mcp_query_requests_total",
        "pg_mcp_llm_calls_total",
        "pg_mcp_llm_tokens_used_total",
        "pg_mcp_sql_rejected_total",
        "pg_mcp_db_query_duration_seconds_count",
    }
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        response.raise_for_status()
    totals = dict.fromkeys(wanted, 0.0)
    for family in text_string_to_metric_families(response.text):
        for sample in family.samples:
            if sample.name in totals:
                totals[sample.name] += sample.value
    return totals


async def run(env_file: Path, output: Path) -> bool:
    env = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    if not env.get("OPENAI_API_KEY") or not env.get("OPENAI_MODEL"):
        raise ValueError("Configure OPENAI_API_KEY and OPENAI_MODEL in the env file")
    env["PYTHONPATH"] = str(PROJECT / "src")
    report: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "model": env["OPENAI_MODEL"],
        "mocked_model": False,
        "transport": "stdio",
        "result_validation_enabled": env.get("VALIDATION_ENABLED", "true").lower() == "true",
        "cases": [],
    }
    # SDK logs may include a private gateway URL. Keep them out of public evidence.
    # The transport can retain the log handle briefly during Windows process shutdown.
    with tempfile.TemporaryDirectory(
        prefix="pg-mcp-live-", ignore_cleanup_errors=True
    ) as temporary:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "pg_mcp"],
            cwd=str(PROJECT),
            env=env,
            log_file=Path(temporary) / "server-stderr.txt",
        )
        async with Client(transport) as client:
            for name, database, question, expected in CASES:
                started = perf_counter()
                result = await client.call_tool(
                    "query", {"question": question, "database": database}, timeout=300
                )
                response = result.data
                passed = bool(response.get("request_id")) and response.get("tokens_used", 0) > 0
                passed = passed and response.get("database") == database
                if isinstance(expected, str):
                    passed = (
                        passed
                        and response.get("success") is False
                        and response.get("error", {}).get("code") == expected
                    )
                else:
                    rows = (response.get("data") or {}).get("rows", [])
                    passed = passed and response.get("success") is True
                    if expected is not None:
                        passed = passed and rows == expected
                    else:
                        passed = passed and bool(rows) and all(
                            set(row) == {"id", "name"} for row in rows
                        )
                report["cases"].append(
                    {
                        "name": name,
                        "question": question,
                        "database": database,
                        "passed": bool(passed),
                        "elapsed_seconds": round(perf_counter() - started, 3),
                        "response": response,
                    }
                )
                save_report(report, output)
                print(f"{'PASS' if passed else 'FAIL'} | {name}", flush=True)
            if env.get("OBSERVABILITY_METRICS_ENABLED", "true").lower() == "true":
                report["metrics"] = await read_metrics(
                    int(env.get("OBSERVABILITY_METRICS_PORT", "9090"))
                )
    save_report(report, output)
    print(f"{report['passed']}/{report['total']} passed; tokens={report['tokens_used']}")
    return report["passed"] == report["total"]


def save_report(report: dict[str, Any], output: Path) -> None:
    """Keep completed responses even if a later request or process shutdown fails."""
    report["passed"] = sum(case["passed"] for case in report["cases"])
    report["total"] = len(CASES)
    report["tokens_used"] = sum(case["response"]["tokens_used"] for case in report["cases"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PROJECT / ".env")
    parser.add_argument("--output", type=Path, default=PROJECT / "docs/evidence/live-demo.json")
    args = parser.parse_args()
    try:
        succeeded = asyncio.run(run(args.env_file, args.output))
    except Exception as error:
        # Provider exceptions can contain endpoint details; never print raw credentials.
        print(f"Live demo did not complete ({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if succeeded else 1)
