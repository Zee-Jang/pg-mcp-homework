"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

import re
from typing import ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {exp.Select, exp.Union, exp.Intersect, exp.Except}

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.With,
        exp.Subquery,
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool | None = None,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Optional list of table names to block access to.
            blocked_columns: Optional list of column names to block access to.
            allow_explain: Whether to allow EXPLAIN statements.
        """
        self.config = config
        self.blocked_tables = {
            t.lower() for t in (config.blocked_tables if blocked_tables is None else blocked_tables)
        }
        self.blocked_columns = {
            c.lower()
            for c in (config.blocked_columns if blocked_columns is None else blocked_columns)
        }
        self.allow_explain = config.allow_explain if allow_explain is None else allow_explain

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # SQLGlot treats EXPLAIN as an opaque Command. Support only a literal
        # plain EXPLAIN SELECT/WITH prefix; parse and validate the entire payload.
        if re.match(r"^\s*EXPLAIN\b", sql, re.IGNORECASE):
            if not self.allow_explain:
                raise SecurityViolationError("EXPLAIN statements are not allowed")
            match = re.match(
                r"^\s*EXPLAIN\s+((?:SELECT|WITH)\b.*)$", sql, re.IGNORECASE | re.DOTALL
            )
            if match is None:
                raise SecurityViolationError("Only plain EXPLAIN SELECT is supported")
            self.validate_or_raise(match.group(1))
            return

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres", error_message_context=0)
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None or isinstance(statement, type(None)):
            raise SQLParseError("No valid SQL statement found")

        # Reject writes anywhere, including data-modifying CTEs, SELECT INTO
        # and locking clauses. Database read-only roles remain the final boundary.
        for node in statement.walk():
            if isinstance(node, (*self.FORBIDDEN_STATEMENT_TYPES, exp.Into, exp.Lock)):
                raise SecurityViolationError(
                    f"{type(node).__name__.upper()} operations are not allowed"
                )

        # Perform security checks
        if error := self._check_statement_type(statement):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        for table in statement.find_all(exp.Table):
            names = [part.name.lower() for part in table.parts]
            full = ".".join(names)
            for blocked in self.blocked_tables:
                # Missing schema qualification cannot disprove a blocked schema.
                if (
                    full == blocked
                    or full.endswith("." + blocked)
                    or (len(names) == 1 and names[0] == blocked.rsplit(".", 1)[-1])
                ):
                    return f"Access to table '{blocked}' is not allowed"
        return None

    @staticmethod
    def _table_matches(table: exp.Table, restriction: str) -> bool:
        full = ".".join(part.name.lower() for part in table.parts)
        return (
            full == restriction
            or full.endswith("." + restriction)
            or (len(table.parts) == 1 and table.name.lower() == restriction.rsplit(".", 1)[-1])
        )

    def _check_blocked_columns(self, statement: exp.Expression) -> str | None:
        """Check all query scopes. Unresolved sensitive provenance fails closed.

        Derived relations are safe for wildcard projection only after their inner
        scopes pass. Without catalog metadata ambiguous sensitive names and
        unknown relations are deliberately rejected rather than guessed.
        """
        if not self.blocked_columns:
            return None
        try:
            for join in statement.find_all(exp.Join):
                if join.args.get("using") or join.args.get("method") == "NATURAL":
                    return "Implicit join column provenance is unsupported under column policy"
            for table in statement.find_all(exp.Table):
                alias = table.args.get("alias")
                if alias is not None and alias.args.get("columns"):
                    return "Table column alias lists are unsupported under column policy"
            scopes = traverse_scope(statement)
            for scope in scopes:
                sources = dict(scope.sources)
                # Unqualified names can fall through to any ancestor when local
                # relations lack that column. Without a catalog, keep every
                # candidate, including ancestors whose aliases are shadowed.
                unqualified_sources = list(scope.sources.values())
                parent = scope.parent
                while parent is not None:
                    unqualified_sources.extend(parent.sources.values())
                    for alias, source in parent.sources.items():
                        sources.setdefault(alias, source)
                    parent = parent.parent
                for node in scope.expression.walk():
                    if node.find_ancestor(exp.Select) is not scope.expression:
                        continue
                    if isinstance(node, exp.Star):
                        if isinstance(node.parent, (exp.Count, exp.Column)):
                            continue
                        candidates = list(scope.sources.values())
                        if self._wildcard_restricted(candidates):
                            return "Wildcard may expose a blocked column"
                    if not isinstance(node, exp.Column):
                        continue
                    qualifier = node.table
                    if (
                        not qualifier
                        and node.name in sources
                        and self._wildcard_restricted([sources[node.name]])
                    ):
                        return "Whole-row expression may expose a blocked column"
                    source = sources.get(qualifier) if qualifier else None
                    if node.db or node.catalog:
                        # A schema-qualified relation name does not resolve to
                        # an unrelated local alias with the same table name.
                        relation = ".".join(part.name.lower() for part in node.parts[:-1])
                        candidates = [
                            item
                            for item in unqualified_sources
                            if isinstance(item, exp.Table) and self._table_matches(item, relation)
                        ]
                    else:
                        candidates = [source] if qualifier else unqualified_sources
                    if node.is_star:
                        if self._wildcard_restricted(candidates):
                            return "Wildcard may expose a blocked column"
                        continue
                    name = node.name.lower()
                    for blocked in self.blocked_columns:
                        parts = blocked.rsplit(".", 1)
                        if name != parts[-1]:
                            continue
                        if len(parts) == 1:
                            return f"Access to column '{blocked}' is not allowed"
                        if not candidates or any(
                            not isinstance(item, exp.Table) or self._table_matches(item, parts[0])
                            for item in candidates
                        ):
                            return f"Blocked or unresolved column '{blocked}' provenance"
        except Exception:
            return "Unable to establish column provenance"
        return None

    def _wildcard_restricted(self, candidates: list[exp.Expression | Scope | None]) -> bool:
        if not candidates:
            return True
        for blocked in self.blocked_columns:
            parts = blocked.rsplit(".", 1)
            for source in candidates:
                if isinstance(source, Scope):
                    continue  # Its projections were checked in its own scope.
                if not isinstance(source, exp.Table) or len(parts) == 1:
                    return True
                if self._table_matches(source, parts[0]):
                    return True
        return False

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for subquery in statement.find_all(exp.Subquery):
            if not isinstance(subquery.this, tuple(self.ALLOWED_STATEMENT_TYPES)):
                return "Subqueries must contain only SELECT statements"
        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e
