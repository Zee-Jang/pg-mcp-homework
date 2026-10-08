"""Render demo responses, pytest results and coverage as HTML reports."""

# Report templates use long lines and Chinese punctuation.
# ruff: noqa: E501, RUF001

import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "evidence"
CSS = """
*{box-sizing:border-box}body{margin:0;background:#eef3f8;color:#152c43;
font-family:'Segoe UI','Microsoft YaHei',sans-serif;font-size:17px;line-height:1.5}
main{width:1440px;padding:46px 64px 36px;margin:auto}
.eyebrow{font-size:14px;letter-spacing:2px;color:#3b6f88;font-weight:700}
h1{font-size:36px;margin:8px 0 8px;letter-spacing:-1px}h2{font-size:20px;margin:0 0 16px}
.sub{color:#567084;font-size:16px;margin-bottom:26px}.stamp{float:right;color:#61788a;font-size:14px}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:22px 0}
.cards.four{grid-template-columns:repeat(4,1fr)}.card,.panel{background:white;
border:1px solid #d6e1ea;border-radius:16px;padding:22px 26px}
.card small{display:block;color:#607789;font-size:15px}.card strong{display:block;
font-size:38px;margin:6px 0 2px;line-height:1.25}.ok{color:#0c7d70}.code{font-family:Consolas,monospace}
.grid{display:grid;grid-template-columns:1.15fr 1fr;gap:20px;margin:20px 0}
table{border-collapse:collapse;width:100%;font-size:16px}th{text-align:left;color:#6b7d8d;
font-size:13px;letter-spacing:.5px;font-weight:600;padding:8px 0 10px}
td{padding:13px 0;border-top:1px solid #eaf0f5;vertical-align:top}
td:last-child{text-align:right}.tag{font-size:13px;color:#0c7d70;background:#e6f5ef;
padding:4px 10px;border-radius:20px;white-space:nowrap}.muted{color:#63798a}
pre{font-family:Consolas,'Microsoft YaHei',monospace;font-size:15px;line-height:1.6;
white-space:pre-wrap;word-break:break-word;margin:0;background:#102b3c;color:#e6f4fa;
border-radius:12px;padding:20px}.note{padding:17px 21px;border-left:4px solid #19a38c;
background:#e2f0ef;font-size:15px;margin-top:22px}.foot{margin-top:20px;font-size:13px;color:#668094}
.compact td{padding:10px 0}.metric{font-family:Consolas,monospace;font-size:14px}
"""


def escape(value: object) -> str:
    """Escape evidence before embedding it in markup."""
    return html.escape(str(value))


def page(title: str, body: str, stamp: str) -> str:
    """Wrap evidence with a shared readable layout."""
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>{CSS}</style><main>"
        f'<span class="stamp">{escape(stamp)}</span>'
        '<div class="eyebrow">PostgreSQL MCP / 测试记录</div>'
        f"<h1>{escape(title)}</h1>{body}"
        '<div class="foot">数据来源：docs/evidence/ · demo.json · pytest.xml · '
        "coverage.json</div></main></html>"
    )


def render() -> None:
    """Build both pages from a completed demonstration and verification run."""
    demo = json.loads((EVIDENCE / "demo.json").read_text(encoding="utf-8"))
    coverage = json.loads((EVIDENCE / "coverage.json").read_text(encoding="utf-8"))
    suites = ET.parse(EVIDENCE / "pytest.xml").getroot().iter("testsuite")  # noqa: S314
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.attrib.get(key, "0"))
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    cases = {case["name"]: case for case in demo["cases"]}
    stamp = demo["generated_at"].replace("T", " ")
    successful = sum(case["passed"] for case in demo["cases"])
    sales = cases["销售库路由"]["response"]["data"]["rows"][0]
    archive = cases["归档库路由"]["response"]["data"]["rows"][0]
    rows = ""
    for name in (
        "普通字段查询",
        "敏感列别名拦截",
        "通配符拦截",
        "敏感表拦截",
        "安全 EXPLAIN",
        "EXPLAIN ANALYZE 拦截",
    ):
        case = cases[name]
        response = case["response"]
        status = "允许" if response["success"] else "拒绝"
        result = "验证通过" if case["passed"] else "验证失败"
        rows += f'<tr><td>{escape(name)}</td><td>{status}</td><td><span class="tag">{result}</span></td></tr>'
    sample = cases["敏感列别名拦截"]["response"]
    rejection = {
        "success": sample["success"],
        "database": sample["database"],
        "error": sample["error"],
        "request_id": sample["request_id"],
    }
    body = f"""
    <div class="sub">同一条统计请求进入不同数据库, 敏感对象在 SQL 执行之前被拒绝</div>
    <div class="cards">
      <div class="card"><small>销售库 · {sales["n"]} 笔订单</small><strong>{sales["total"]:,.0f}</strong><small class="code">{escape(sales["db"])}</small></div>
      <div class="card"><small>归档库 · {archive["n"]} 笔订单</small><strong>{archive["total"]:,.0f}</strong><small class="code">{escape(archive["db"])}</small></div>
      <div class="card"><small>场景通过</small><strong class="ok">{successful} / {len(cases)}</strong><small>正常查询、异常处理与恢复</small></div>
    </div>
    <div class="panel"><h2>查询 SQL</h2><pre>{escape(cases["销售库路由"]["response"]["generated_sql"])}</pre></div>
    <div class="grid">
      <div class="panel"><h2>访问控制与 EXPLAIN</h2><table><tr><th>场景</th><th>行为</th><th>结果</th></tr>{rows}</table></div>
      <div class="panel"><h2>密码列查询响应</h2><pre>{escape(json.dumps(rejection, ensure_ascii=False, indent=2))}</pre></div>
    </div>
    <div class="note">PostgreSQL + MCP 集成测试，模型输出固定（Mock）<br>数据库: {escape(demo["postgres_version"])}</div>
    """
    (EVIDENCE / "01-demo.html").write_text(
        page("双库路由与访问控制", body, stamp), encoding="utf-8"
    )

    security = next(v for k, v in coverage["files"].items() if k.endswith("sql_validator.py"))
    retry = cases["临时故障退避重试"]
    limit = cases["超限请求拒绝"]
    lint = (EVIDENCE / "lint.txt").read_text(encoding="utf-8-sig")
    types = (EVIDENCE / "types.txt").read_text(encoding="utf-8-sig")
    lint_status = "通过" if "All checks passed!" in lint else "查看原始结果"
    type_status = "通过" if "Success: no issues found" in types else "查看原始结果"
    metrics = "".join(
        f'<tr><td class="metric">{escape(name)}</td><td>{value:g}</td></tr>'
        for name, value in sorted(demo["metrics_delta"].items())
    )
    test_summary = (
        f"{passed} passed, {totals['skipped']} skipped\n"
        f"{totals['failures']} failures, {totals['errors']} errors\n\n"
        f"ruff: {lint_status}\nmypy: {type_status}"
    )
    body2 = f"""
    <div class="sub">回归测试、真实 PostgreSQL 集成测试、运行指标与请求追踪</div>
    <div class="cards four">
      <div class="card"><small>测试通过</small><strong class="ok">{passed}</strong><small>{totals["skipped"]} 项外部依赖测试跳过</small></div>
      <div class="card"><small>失败 / 错误</small><strong>{totals["failures"]} / {totals["errors"]}</strong><small>JUnit 测试结果</small></div>
      <div class="card"><small>整体覆盖率</small><strong>{coverage["totals"]["percent_covered"]:.2f}%</strong><small>行与分支联合统计</small></div>
      <div class="card"><small>SQL 校验覆盖率</small><strong>{security["summary"]["percent_covered"]:.2f}%</strong><small>行与分支联合统计</small></div>
    </div>
    <div class="grid">
      <div class="panel"><h2>运行指标增量</h2><table class="compact"><tr><th>Prometheus 指标</th><th>增量</th></tr>{metrics}</table></div>
      <div class="panel"><h2>测试结果</h2><pre>{escape(test_summary)}</pre><p class="muted">46 项旧外部模型测试未启用；真实模型联调单独记录于 live-demo.json。</p></div>
    </div>
    <div class="panel"><h2>异常恢复与链路追踪</h2><table class="compact">
      <tr><td>临时故障退避重试</td><td>{retry["model_calls"]} 次模型测试组件调用后查询成功</td></tr>
      <tr><td>请求并发槽超限</td><td class="code">{escape(limit["response"]["error"]["code"])}</td></tr>
      <tr><td>限流结束后恢复</td><td>{"通过" if cases["限流后恢复查询"]["passed"] else "失败"}</td></tr>
      <tr><td>请求 ID 隔离及传播</td><td>{"通过" if demo["request_tracing_passed"] else "失败"}</td></tr>
      <tr><td>重试请求 ID</td><td class="code">{escape(retry["response"]["request_id"])}</td></tr>
    </table></div>
    <div class="note">PostgreSQL + MCP 集成测试，模型输出固定（Mock）<br>覆盖率：pytest-cov，统计行与分支。</div>
    """
    (EVIDENCE / "02-verification.html").write_text(
        page("回归测试与运行观测", body2, stamp), encoding="utf-8"
    )
    print("Rendered 01-demo.html and 02-verification.html from saved execution evidence.")


if __name__ == "__main__":
    render()
