"""Render saved live MCP responses as two readable reports."""

# Report templates use Chinese punctuation and long HTML lines.
# ruff: noqa: E501, RUF001

import json
from datetime import datetime, timedelta, timezone

from render_evidence import CSS, EVIDENCE, escape


def page(title: str, body: str, stamp: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style><main>"
        f'<span class="stamp">{escape(stamp)}</span>'
        '<div class="eyebrow">PostgreSQL MCP / 运行结果</div>'
        f"<h1>{escape(title)}</h1>{body}"
        '<div class="foot">数据来源：docs/evidence/live-demo.json · '
        '由 scripts/demo_live.py 调用独立 stdio 服务进程记录</div></main></html>'
    )


def render() -> None:
    report = json.loads((EVIDENCE / "live-demo.json").read_text(encoding="utf-8"))
    cases = report["cases"]
    stamp = datetime.fromisoformat(report["timestamp"]).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M UTC+8")
    cards = f"""
    <div class="cards">
      <div class="card"><small>SQL 生成服务</small><strong style="font-size:29px">{escape(report["model"])}</strong><small>兼容接口</small></div>
      <div class="card"><small>验证场景</small><strong class="ok">{report["passed"]} / {report["total"]}</strong><small>双库统计、普通字段、敏感列</small></div>
      <div class="card"><small>调用用量</small><strong>{report["tokens_used"]:,}</strong><small>服务返回的累计用量</small></div>
    </div>"""
    queries = ""
    for case in cases[:2]:
        response = case["response"]
        queries += f"""
        <div class="panel"><h2>{escape(case["database"])}</h2>
        <p class="muted">模型生成 SQL</p><pre>{escape(response.get("generated_sql"))}</pre>
        <p class="muted">数据库返回</p><pre>{escape(json.dumps(response.get("data", {}).get("rows"), ensure_ascii=False, indent=2))}</pre>
        <p class="muted">耗时 {case["elapsed_seconds"]:.2f}s · {response["tokens_used"]:,} tokens · {"PASS" if case["passed"] else "FAIL"}</p>
        <div class="foot code">request_id: {escape(response["request_id"])}</div></div>"""
    body = f"""
    <div class="sub">自然语言 → SQL 生成 → 安全校验 → PostgreSQL → MCP 响应</div>
    {cards}
    <div class="panel"><h2>同一个问题，指定不同数据库</h2>{escape(cases[0]["question"])}</div>
    <div class="grid" style="grid-template-columns:1fr 1fr">{queries}</div>
    <div class="note">PostgreSQL 真实测试数据：销售库 3 笔 / 400，归档库 2 笔 / 1000。<br>本次启用模型 SQL 生成，结果复核开关：{"开启" if report["result_validation_enabled"] else "关闭"}。</div>"""
    (EVIDENCE / "03-live-query.html").write_text(
        page("自然语言查询与双库路由", body, stamp), encoding="utf-8"
    )
    normal, denied = cases[2:4]
    normal_response, denied_response = normal["response"], denied["response"]
    security_result = {
        key: denied_response[key]
        for key in ("success", "database", "error", "tokens_used", "request_id")
    }
    metrics = "".join(
        f'<tr><td class="metric">{escape(name)}</td><td>{value:g}</td></tr>'
        for name, value in sorted(report.get("metrics", {}).items())
    )
    body2 = f"""
    <div class="sub">SQL 生成服务输出查询语句，应用校验器限制密码字段访问</div>
    <div class="grid" style="grid-template-columns:1fr 1.15fr">
      <div class="panel"><h2>普通字段 · {"PASS" if normal["passed"] else "FAIL"}</h2>
      <p>{escape(normal["question"])}</p><pre>{escape(normal_response.get("generated_sql"))}</pre>
      <p class="muted">PostgreSQL 返回</p><pre>{escape(json.dumps(normal_response.get("data", {}).get("rows"), ensure_ascii=False, indent=2))}</pre>
      <p class="muted">{normal["elapsed_seconds"]:.2f}s · {normal_response["tokens_used"]:,} tokens</p></div>
      <div class="panel"><h2>密码字段 · {"PASS" if denied["passed"] else "FAIL"}</h2>
      <p>{escape(denied["question"])}</p><pre>{escape(json.dumps(security_result, ensure_ascii=False, indent=2))}</pre>
      <p class="muted">{denied["elapsed_seconds"]:.2f}s · MCP 返回实际错误码</p></div>
    </div>
    <div class="panel"><h2>本次服务进程的 Prometheus 指标</h2><table class="compact"><tr><th>指标</th><th>数值</th></tr>{metrics}</table></div>
    <div class="note">受限查询返回 security_violation；正常查询返回数据库数据。<br>数据库只读账号同时限制 password 列和 audit_log 表的访问权限。</div>"""
    (EVIDENCE / "04-live-security.html").write_text(
        page("普通查询与敏感字段拦截", body2, stamp), encoding="utf-8"
    )
    print("Rendered live model reports from live-demo.json.")


if __name__ == "__main__":
    render()
