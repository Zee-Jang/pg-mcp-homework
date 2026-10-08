"""Read-only request pipeline with explicit database routing and bounded LLM calls."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, TypeVar

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import (
    get_llm_tokens,
    get_request_id,
    get_tracing_logger,
    request_context,
)
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_tracing_logger(__name__)
T = TypeVar("T")


class QueryOrchestrator:
    """Route schema and executor together; legacy executor is single-database only."""

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        sql_executor: SQLExecutor | None = None,
        *,
        sql_executors: dict[str, SQLExecutor] | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executor = sql_executor
        self.sql_executors = dict(sql_executors or {})
        if not self.sql_executors and len(pools) == 1 and sql_executor is not None:
            self.sql_executors[next(iter(pools))] = sql_executor
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.rate_limiter = rate_limiter or MultiRateLimiter(
            query_limit=resilience_config.query_concurrency,
            llm_limit=resilience_config.llm_concurrency,
        )
        self.metrics = metrics
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Reuse an existing request context and observe success and failed paths."""
        async with request_context(get_request_id()) as request_id:
            database = None
            status = "error"
            started = time.monotonic()
            try:
                database = self._resolve_database(request.database)
                if database not in self.sql_executors:
                    raise DatabaseError("No executor configured for selected database")
                if len(request.question) > self.validation_config.max_question_length:
                    raise PgMcpError(
                        "Question exceeds configured length", ErrorCode.QUESTION_TOO_LONG
                    )
                acquired = await self.rate_limiter.query_limiter.acquire(
                    timeout=self.resilience_config.queue_timeout
                )
                if not acquired:
                    raise RateLimitExceededError("Query concurrency queue timeout")
                try:
                    response = await self._execute_resolved(request, database)
                    status = "success"
                    response.tokens_used = get_llm_tokens()
                    response.request_id = request_id
                    response.database = database
                    return response
                finally:
                    self.rate_limiter.query_limiter.release()
            except PgMcpError as error:
                logger.warning("Query failed", extra={"error_code": error.code.value})
                return QueryResponse(
                    success=False,
                    error=ErrorDetail(
                        code=error.code.value,
                        message=error.message,
                        details=error.details,
                    ),
                    confidence=0,
                    tokens_used=get_llm_tokens(),
                    request_id=request_id,
                    database=database,
                )
            except Exception as error:
                logger.error("Unexpected query failure", extra={"error_type": type(error).__name__})
                return QueryResponse(
                    success=False,
                    error=ErrorDetail(
                        code=ErrorCode.INTERNAL_ERROR.value,
                        message="Internal server error",
                        details={"error_type": type(error).__name__},
                    ),
                    confidence=0,
                    tokens_used=get_llm_tokens(),
                    request_id=request_id,
                    database=database,
                )
            finally:
                if self.metrics is not None:
                    self.metrics.increment_query_request(status, database or "unresolved")
                    self.metrics.query_duration.observe(time.monotonic() - started)

    async def _execute_resolved(self, request: QueryRequest, database: str) -> QueryResponse:
        logger.info("Starting query", extra={"database": database})
        schema = self.schema_cache.get(database)
        if schema is None:
            try:
                schema = await self.schema_cache.load(database, self.pools[database])
            except Exception as error:
                raise SchemaLoadError("Failed to load schema", {"database": database}) from error
        logger.debug("Schema loaded", extra={"database": database, "tables": len(schema.tables)})
        sql, validation, tokens = await self._generate_sql_with_retry(
            request.question, schema, get_request_id() or ""
        )
        if request.return_type == ReturnType.SQL:
            return QueryResponse(
                success=True, generated_sql=sql, validation=validation, tokens_used=tokens
            )
        logger.debug("Executing SQL", extra={"database": database})
        started = time.monotonic()
        try:
            rows, total = await self.sql_executors[database].execute(sql)
        finally:
            if self.metrics is not None:
                self.metrics.observe_db_query_duration(time.monotonic() - started)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("SQL executed", extra={"database": database, "row_count": len(rows)})
        confidence = await self._validate_results_safely(
            request.question, sql, rows, total, get_request_id() or ""
        )
        return QueryResponse(
            success=True,
            generated_sql=sql,
            validation=validation,
            confidence=confidence,
            data=QueryResult(
                columns=list(rows[0]) if rows else [],
                rows=rows,
                row_count=len(rows),
                execution_time_ms=elapsed_ms,
            ),
            tokens_used=tokens,
        )

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _call_llm(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """Retry only classified transient failures; release slots before sleeping."""
        if not self.circuit_breaker.allow_request():
            raise LLMError("LLM temporarily unavailable (circuit breaker open)")
        config = self.resilience_config
        for attempt in range(config.max_retries + 1):
            acquired = await self.rate_limiter.llm_limiter.acquire(timeout=config.queue_timeout)
            if not acquired:
                raise RateLimitExceededError("LLM concurrency queue timeout")
            started = time.monotonic()
            tokens_before = get_llm_tokens()
            try:
                if self.metrics is not None:
                    self.metrics.increment_llm_call(operation)
                value = await call()
                self.circuit_breaker.record_success()
                return value
            except Exception as error:
                retryable = isinstance(error, (LLMTimeoutError, TimeoutError, ConnectionError)) or (
                    isinstance(error, LLMError) and error.details.get("retryable") is True
                )
                if not retryable or attempt == config.max_retries:
                    self.circuit_breaker.record_failure()
                    if isinstance(error, PgMcpError):
                        raise
                    raise LLMError(
                        "LLM call failed unexpectedly", details={"error_type": type(error).__name__}
                    ) from error
            finally:
                self.rate_limiter.llm_limiter.release()
                if self.metrics is not None:
                    self.metrics.observe_llm_latency(operation, time.monotonic() - started)
                    self.metrics.increment_llm_tokens(operation, get_llm_tokens() - tokens_before)
            delay = min(config.retry_delay * config.backoff_factor**attempt, config.max_retry_delay)
            logger.warning("Retrying transient LLM failure", extra={"attempt": attempt + 1})
            await asyncio.sleep(delay)
        raise LLMError("Retry budget exhausted")  # pragma: no cover

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int]:
        previous_sql = None
        feedback = None
        for attempt in range(self.resilience_config.max_retries + 1):
            logger.debug("Generating SQL", extra={"request_id": request_id, "attempt": attempt + 1})
            sql = await self._call_llm(
                "generate_sql",
                partial(
                    self.sql_generator.generate,
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=feedback,
                ),
            )
            try:
                self.sql_validator.validate_or_raise(sql)
            except SecurityViolationError:
                if self.metrics is not None:
                    self.metrics.increment_sql_rejected("security_violation")
                raise  # A denied policy must never receive model correction retries.
            except SQLParseError as error:
                if attempt == self.resilience_config.max_retries:
                    raise
                previous_sql, feedback = sql, str(error)
                continue
            logger.info("SQL validated", extra={"request_id": request_id})
            return sql, ValidationResult(is_valid=True, is_select=True), get_llm_tokens()
        raise LLMError("SQL generation failed")  # pragma: no cover

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        if not self.validation_config.enabled:
            return 100
        try:
            logger.debug("Validating results", extra={"request_id": request_id})
            result = await self._call_llm(
                "validate_result",
                lambda: self.result_validator.validate(
                    question=question,
                    sql=sql,
                    results=results,
                    row_count=row_count,
                ),
            )
            return result.confidence
        except RateLimitExceededError:
            raise
        except Exception as error:
            logger.warning("Result validation failed", extra={"error_type": type(error).__name__})
            return 100

    @staticmethod
    def _get_current_time_ms() -> float:
        return time.monotonic() * 1000
