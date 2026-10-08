# 实现与测试记录

## 数据库路由

原项目可以按库名读取 Schema，但 SQL 始终交给同一个执行器。启动时改为根据 `DATABASES` 分别创建连接池和执行器，请求中的库名同时决定 Schema 和执行器。未配置的库名直接拒绝；配置多个数据库时，省略库名也会返回错误，不进入模型调用。

使用两套订单数据检查路由：`homework_sales` 返回 3 笔订单、总额 400，`homework_archive` 返回 2 笔订单、总额 1000。响应中的 `current_database()` 与请求库名一致。

## SQL 访问控制

`blocked_tables`、`blocked_columns` 和 `allow_explain` 从配置传入 SQL 校验器。SQLGlot 解析后检查表和列的来源，覆盖别名、嵌套查询、通配符、整行引用以及关联子查询。未限定表名的列需要考虑外层作用域；带 Schema 的列引用不能被同名局部别名遮蔽。

EXPLAIN 默认关闭。开启后只接受校验通过的普通 SELECT/WITH，拒绝 ANALYZE、写语句及未支持的选项。SQL 执行保留只读事务，超时、search_path 和角色设置改为 SET LOCAL，避免影响连接池中的后续请求。

## 请求处理

查询与模型调用分别使用并发限制器，等待超时返回 `rate_limit_exceeded`，异常或取消后释放槽位。模型的超时、连接失败、429 和 5xx 错误采用有上限的指数退避；认证失败和安全拒绝不重试。

通过 ContextVar 传递 request_id，响应和各阶段日志可以对应。请求数、模型调用次数、耗时和安全拒绝次数写入 Prometheus。日志输出到 stderr，避免影响 stdio 协议。

响应模型只保留一个 to_dict 方法，检查成功、错误和数据字段的一致性。失败响应也保留已统计的 token 用量。环境变量和 .env 中的扁平配置映射到各子配置，缓存开关和问题长度限制接入请求流程。

`CACHE_MAX_SIZE` 限制 Schema 缓存条目数。容量满时淘汰最久未访问的库，命中缓存不会延长 Schema 的有效期；刷新已缓存的库不会误删其他库。新增测试覆盖容量淘汰、读取后的保留顺序和满容量刷新。

增加 `OPENAI_BASE_URL`，SQL 生成器和结果复核器使用相同的兼容接口地址；未设置时使用 SDK 默认地址。配置加载与两个客户端的地址传递分别有回归测试。

## 测试结果

环境：Windows、Python 3.14.6、PostgreSQL 17.11。

| 检查项 | 结果 |
| --- | --- |
| pytest | 347 通过，46 跳过，0 失败 |
| PostgreSQL / MCP 集成测试 | 14 通过，包含在上述数量中 |
| 固定模型输出的回归演示 | 15/15 通过 |
| Qwen3.6-27B 真实模型联调 | 4/4 通过，单独运行 |
| 整体覆盖率（行与分支） | 90.57% |
| SQL 校验器覆盖率（行与分支） | 96.43% |
| Ruff | 通过 |
| Mypy | 31 个源文件通过 |

修改前的基线为 247 个单元测试通过、1 个失败，Ruff 和 Mypy 各有 4 个问题。

[pytest 输出](evidence/pytest.txt) · [JUnit XML](evidence/pytest.xml) · [覆盖率](evidence/coverage.json) · [Ruff](evidence/lint.txt) · [Mypy](evidence/types.txt)

pytest 中的 PostgreSQL 测试和 15 个回归演示场景使用固定模型输出（Mock）。46 项原有外部模型测试未启用；真实模型通过 `demo_live.py` 单独联调。PostgreSQL 在本地独立启动；Docker Compose 已检查配置，尚未验证容器启动。

2026-10-08 使用 Qwen3.6-27B，经独立 stdio 服务进程完成以下查询：

| 问题 | 目标数据库 | 实际结果 |
| --- | --- | --- |
| 统计订单数量和总金额 | homework_sales | 3 笔，400 |
| 统计订单数量和总金额 | homework_archive | 2 笔，1000 |
| 查询用户 id 和 name | homework_sales | id=1，name=Alice |
| 查询用户 password | homework_sales | security_violation，未执行 SQL |

监控记录 4 次模型调用、3 次数据库查询和 1 次安全拒绝，用量为 5561 tokens，单次耗时约 21–37 秒。结果复核关闭，SQL 生成使用真实模型。原始响应、生成的 SQL、request_id、耗时和指标保存在 [live-demo.json](evidence/live-demo.json)。

联调时曾遇到模型有 token 用量但正文为空的响应，配置由默认 2000-token 预算调整为 `OPENAI_MAX_TOKENS=4096` 后，上述四个场景通过。接口地址通过 `OPENAI_BASE_URL` 配置，访问密钥只存放在本地 `.env`。

主要回归场景：

- 相同查询切换数据库，返回不同的订单数量和总额。
- 普通字段可查询，密码列、受限通配符和审计表在执行前被拒绝。
- 关联子查询中的外层敏感列、带 Schema 的同名别名绕过被拒绝。
- 普通 EXPLAIN 可执行，EXPLAIN ANALYZE 和写语句被拒绝。
- 模拟首次模型超时，退避后恢复；安全拒绝不重试。
- 并发槽占满时请求被拒绝，释放后恢复查询。
- 以独立进程启动 `python -m pg_mcp`，能通过 stdio 列出 query 工具并调用。
- request_id 在请求间隔离，失败响应保留 token 用量。

演示响应见 [demo.json](evidence/demo.json)，阶段日志见 [demo-trace.txt](evidence/demo-trace.txt)。

## 复现

```powershell
uv sync --extra dev
docker compose -f docker-compose.homework.yml up -d --wait
uv run python scripts/demo_homework.py
$env:HOMEWORK_PG_TESTS='1'
uv run pytest --cov=src --cov-report=term-missing --cov-report=json:docs/evidence/coverage.json --junitxml=docs/evidence/pytest.xml *> docs/evidence/pytest.txt
uv run ruff check src tests scripts *> docs/evidence/lint.txt
uv run mypy src *> docs/evidence/types.txt
uv run python scripts/render_evidence.py

# 配置 .env 的模型参数后运行真实模型联调
uv run python scripts/demo_live.py
uv run python scripts/render_live_evidence.py
```

测试输出写入 `docs/evidence/`。`render_evidence.py` 读取保存的结果，生成 `01-demo.html` 和 `02-verification.html`；`render_live_evidence.py` 生成 `03-live-query.html` 和 `04-live-security.html`。同名 PNG 为这些报告的浏览器截图。

## 已知限制

- 限流按单进程并发数计算，没有实现按用户或每秒请求量的配额。
- 列来源不明确时采用保守拒绝，部分复杂查询可能无法执行。视图、自定义函数和扩展内部权限仍需由数据库角色约束。
- `tokens_used=0` 表示未采集到用量。结果复核关闭或遇到非资源类错误降级时，沿用原项目的 `confidence=100` 默认值，不能用于衡量准确率。
