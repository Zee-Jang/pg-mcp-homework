# PostgreSQL MCP 功能补全与质量改进

AI 编程实战营第 2 次作业 · 研发岗

基于[课程 pg-mcp 项目](https://github.com/tyrchen/geektime-bootcamp-ai/tree/master/w5/pg-mcp)补齐代码审查指出的功能。基础提交：`4f1be7c93cb14858d91e7aaf33d5f44388cc4302`。原说明保留在 [README.course.md](README.course.md)，本次实现以本文为准。

## 作业要求与实现对应

| 课程问题 | 本次改动 | 验证方式 |
| --- | --- | --- |
| 多库始终使用单个执行器 | 按数据库名称同时选择 Schema 和执行器；拒绝未知库与省略库名的歧义请求 | 真实双库的订单总额分别为 400 和 1000 |
| 表、列、EXPLAIN 策略未生效 | 安全配置接入实际校验器；AST 检查别名、子查询、通配符、整行引用；受限 EXPLAIN | 安全回归用例及 MCP 拒绝响应 |
| 限流、退避未接入 | 请求和 LLM 并发限流、有限等待、临时失败指数退避；安全拒绝和认证错误不重试 | 并发、取消、重试及恢复测试 |
| 指标、追踪未接入 | ContextVar 请求 ID、阶段日志、请求数、模型调用数、耗时与安全拒绝计数 | 真实运行日志与 Prometheus 指标 |
| 响应和配置缺陷 | 唯一 to_dict、稳定 tokens_used、响应状态校验、配置加载与开关接入 | 模型、配置和入口回归测试 |
| 测试不足 | 保留原单元测试，补充缺陷回归和真实 PostgreSQL/MCP 集成测试 | docs/evidence 原始输出 |

诊断日志输出到 **stderr**，避免干扰 MCP stdio 协议。

## 快速复现：无需大模型密钥

需要 Python **3.14+**、uv 和运行中的 Docker。在本 README 所在目录执行：

```powershell
uv sync --extra dev
docker compose -f docker-compose.homework.yml up -d --wait
uv run python scripts/demo_homework.py
```

Docker 创建作业专用 PostgreSQL，绑定 `127.0.0.1:55439`：

- `homework_sales`：3 笔订单，总额 **400**。
- `homework_archive`：2 笔订单，总额 **1000**。
- `homework_reader`：非超级用户；数据库授权也禁止读取密码列和审计表。

初始化脚本仅用于新建的作业实例。配置中的密码是公开演示数据。端口冲突时，在启动和运行脚本的同一终端设置 `$env:HOMEWORK_PG_PORT='55440'`。

**演示边界：使用真实 PostgreSQL、真实 MCP 内存传输和服务生命周期，仅将外部大模型替换为固定输出的测试组件。** SQL 校验、路由、执行、限流、退避、统计均走实际代码。没有调用付费 API，不能据此声称已完成真实模型端到端验证。

脚本执行 15 个场景并断言结果，失败返回非零退出码，保存：

- [机器可读结果](docs/evidence/demo.json)
- [终端输出](docs/evidence/demo-console.txt)
- [带请求 ID 的阶段日志](docs/evidence/demo-trace.txt)

## 测试与检查

```powershell
# 无外部服务：外部集成测试明确显示为 skipped
uv run pytest -q

# 真实 PostgreSQL，无外部模型
$env:HOMEWORK_PG_TESTS='1'
uv run pytest tests/integration/test_homework_postgres.py -q

# 全套覆盖率（包含已启用的 PostgreSQL 测试）
uv run pytest --cov=src --cov-report=term-missing --cov-report=json:docs/evidence/coverage.json
uv run ruff check src tests scripts
uv run mypy src
```

本次实际结果及截图见 [验证记录和提交说明](docs/homework.md)。基线原始输出也已保留：原项目 247 个单元测试通过、1 个失败；静态检查与类型检查各有 4 个问题。

课程原有依赖真实模型的 integration/e2e 测试需设置 `PG_MCP_LIVE_TESTS=1` 才执行，且必须配好数据库和密钥。本次验收以新增、有明确结果断言的测试为依据。

## 连接真实模型

```powershell
Copy-Item .env.homework.example .env
# 编辑 .env，填写自己的 API key 和账号可用的模型名称
uv run python -m pg_mcp
```

`.env` 已被 Git 忽略。`python -m pg_mcp` 启动 stdio MCP 服务，单独启动后等待输入属于正常状态，需要客户端发起调用。根目录的 `main.py` 是上游遗留的加法示例，请使用这里的模块入口。

客户端配置，替换项目绝对路径：

```json
{
  "mcpServers": {
    "homework-postgres": {
      "command": "uv",
      "args": ["--directory", "D:/your/path/pg-mcp", "run", "python", "-m", "pg_mcp"]
    }
  }
}
```

查询时明确指定 `homework_sales` 或 `homework_archive`。开启监控后访问 `http://127.0.0.1:19139/metrics`。本次没有提供真实模型密钥，真实模型生成 SQL 的效果需配置后另行验证。

## 关键配置

| 配置 | 行为 |
| --- | --- |
| DATABASES | JSON 数组，优先于旧 DATABASE_* 单库配置；名称不可重复 |
| SECURITY_BLOCKED_TABLES / SECURITY_BLOCKED_COLUMNS | JSON 数组；来源不明确的敏感查询保守拒绝 |
| SECURITY_ALLOW_EXPLAIN | 默认关闭；开启仍拒绝 ANALYZE、写语句和不支持的选项 |
| SECURITY_ALLOW_WRITE_OPERATIONS | 必须为 false，启用写操作会报配置错误 |
| RESILIENCE_QUERY_CONCURRENCY / RESILIENCE_LLM_CONCURRENCY | 同时处理的请求/模型调用上限，属于并发限流 |
| RESILIENCE_QUEUE_TIMEOUT | 等待并发槽的时限，超时返回 rate_limit_exceeded |
| RESILIENCE_MAX_RETRIES / RETRY_DELAY / BACKOFF_FACTOR / MAX_RETRY_DELAY | 次数、初始延迟、倍率、延迟上限；完整名称均加 RESILIENCE_ 前缀 |
| CACHE_ENABLED / VALIDATION_MAX_QUESTION_LENGTH | 缓存开关与问题长度限制 |
| OBSERVABILITY_METRICS_ENABLED | 实际统计及指标服务开关 |

SQL 策略采用保守的应用层检查。视图、自定义函数和扩展的内部行为需由数据库权限约束；复杂来源不明的查询可能被拒绝。`tokens_used=0` 表示本管线未采集到用量，不代表真实调用免费。

## 交付文件

- `src/`、`tests/`：实现及测试。
- `scripts/demo_homework.py`：可复现的实际调用演示。
- `docker-compose.homework.yml`、`scripts/init-demo.sql`：双库测试环境。
- `docs/homework.md`：作业说明、验证结果和提交方法。
- `docs/evidence/`：实际运行输出与两张可上传效果图。

提交时填写自己的 GitHub / Gitee 仓库链接，并上传两张效果图。
