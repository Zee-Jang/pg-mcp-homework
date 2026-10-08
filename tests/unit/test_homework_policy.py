"""Security and configuration regressions for the homework completion."""

import json

import pytest
from pydantic import ValidationError

from pg_mcp.config.settings import SecurityConfig, Settings, ValidationConfig
from pg_mcp.models.query import ErrorDetail, QueryResponse
from pg_mcp.services.sql_validator import SQLValidator


@pytest.fixture(autouse=True)
def simulated_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-offline-test")


def test_database_list_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASES", json.dumps([{"name": "one"}, {"name": "two"}]))
    settings = Settings(_env_file=None)
    assert [db.name for db in settings.database_configs] == ["one", "two"]


def test_duplicate_database_names_rejected():
    with pytest.raises(ValidationError, match="unique"):
        Settings(databases=[{"name": "one"}, {"name": "one"}], _env_file=None)


def test_flat_dotenv_settings_propagate(tmp_path):
    env = tmp_path / ".env"
    env.write_text('DATABASE_NAME=dotenv_db\nSECURITY_BLOCKED_TABLES=["secrets"]\n')
    settings = Settings(_env_file=env)
    assert settings.database_configs[0].name == "dotenv_db"
    assert not SQLValidator(settings.security).validate("SELECT * FROM secrets")[0]


def test_write_enablement_rejected():
    with pytest.raises(ValidationError, match="read-only"):
        SecurityConfig(allow_write_operations=True)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.password FROM public.users u",
        "SELECT u.* FROM public.users u",
        "SELECT * FROM public.users",
        "SELECT x.password FROM (SELECT password FROM public.users) x",
        "WITH x AS (SELECT password FROM users) SELECT * FROM x",
        "SELECT password FROM users JOIN accounts ON users.id = accounts.id",
        "SELECT (SELECT u.password) FROM users u",
    ],
)
def test_restricted_columns_never_escape(sql):
    assert not SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.id FROM users u",
        "SELECT COUNT(*) FROM users",
        "SELECT a.* FROM accounts a JOIN users u ON a.id=u.id",
        "WITH x AS (SELECT id FROM users) SELECT x.* FROM x",
    ],
)
def test_safe_projection_and_count_allowed(sql):
    assert SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM public.secrets",
        "SELECT * FROM (SELECT id FROM public.secrets) x",
    ],
)
def test_qualified_table_policy(sql):
    assert not SQLValidator(SecurityConfig(), blocked_tables=["public.secrets"]).validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN ANALYZE SELECT 1",
        "EXPLAIN DELETE FROM users",
        "EXPLAIN (FORMAT JSON) SELECT 1",
        "EXPLAIN SELECT pg_sleep(1)",
        "EXPLAIN SELECT u.password FROM users u",
    ],
)
def test_explain_inner_policy(sql):
    assert not SQLValidator(
        SecurityConfig(), blocked_columns=["users.password"], allow_explain=True
    ).validate(sql)[0]


def test_plain_explain_select_opt_in():
    assert not SQLValidator(SecurityConfig()).validate("EXPLAIN SELECT 1")[0]
    assert SQLValidator(SecurityConfig(), allow_explain=True).validate("EXPLAIN SELECT 1")[0]


def test_empty_token_usage_is_stable():
    assert QueryResponse(success=True).tokens_used == 0
    assert QueryResponse(success=True).to_dict()["tokens_used"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"success": False},
        {"success": True, "error": ErrorDetail(code="failure", message="failed")},
    ],
)
def test_response_state_coherent(kwargs):
    with pytest.raises(ValidationError):
        QueryResponse(**kwargs)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u FROM users u",
        "SELECT row_to_json(u) FROM users u",
        "SELECT to_jsonb(users) FROM users",
        "SELECT safe FROM users AS u(id,name,email,safe)",
        "SELECT id FROM users NATURAL JOIN accounts",
        "SELECT id FROM users JOIN accounts USING(password)",
    ],
)
def test_implicit_sensitive_projections_and_joins_rejected(sql):
    assert not SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH gone AS (DELETE FROM users RETURNING id) SELECT * FROM gone",
        "SELECT id INTO copied_users FROM users",
        "SELECT id FROM users FOR UPDATE",
    ],
)
def test_nested_and_select_write_forms_rejected(sql):
    assert not SQLValidator(SecurityConfig()).validate(sql)[0]


def test_legacy_confidence_threshold_alias_effective():
    assert ValidationConfig(min_confidence_score=88).confidence_threshold == 88
    assert ValidationConfig(confidence_threshold=88).min_confidence_score == 88
    with pytest.raises(ValidationError, match="conflict"):
        ValidationConfig(min_confidence_score=88, confidence_threshold=70)


@pytest.mark.parametrize(
    "sql", ["SELECT unknown.* FROM users", "SELECT *", "SELECT x.password FROM accounts a"]
)
def test_unknown_column_provenance_fails_closed(sql):
    assert not SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]


def test_nested_union_readonly_query_permitted():
    sql = "SELECT id FROM (SELECT id FROM users UNION SELECT id FROM accounts) safe"
    assert SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]


@pytest.mark.parametrize(
    "blocked, sql",
    [
        (
            "orders.customer",
            "SELECT (SELECT customer FROM users LIMIT 1) AS leaked FROM orders ORDER BY id",
        ),
        ("users.password", "SELECT (SELECT password FROM orders LIMIT 1) AS leaked FROM users"),
        ("users.password", "SELECT (SELECT password FROM orders u LIMIT 1) FROM users u"),
        (
            "users.password",
            "SELECT (SELECT (SELECT password FROM orders LIMIT 1) "
            "FROM accounts LIMIT 1) FROM users",
        ),
    ],
)
def test_unqualified_sensitive_column_considers_all_ancestor_scopes(blocked, sql):
    """PostgreSQL can resolve a missing local column against an outer relation."""
    assert not SQLValidator(SecurityConfig(), blocked_columns=[blocked]).validate(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (SELECT o.amount FROM orders o LIMIT 1) FROM users u",
        "SELECT (SELECT o.password FROM orders o LIMIT 1) FROM users u",
        "SELECT (SELECT u.name FROM orders o LIMIT 1) FROM users u",
    ],
)
def test_explicit_safe_correlated_projections_remain_permitted(sql):
    assert SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]


@pytest.mark.parametrize(
    "blocked, sql",
    [
        (
            "orders.customer",
            "SELECT (SELECT public.orders.customer FROM users AS orders LIMIT 1) "
            "AS leaked FROM public.orders ORDER BY id",
        ),
        (
            "users.password",
            "SELECT (SELECT public.users.password FROM orders AS users LIMIT 1) "
            "AS leaked FROM public.users",
        ),
    ],
)
def test_schema_qualified_sensitive_columns_ignore_shadowing_aliases(blocked, sql):
    assert not SQLValidator(SecurityConfig(), blocked_columns=[blocked]).validate(sql)[0]


def test_schema_qualified_safe_relation_does_not_inherit_outer_policy():
    sql = "SELECT (SELECT public.orders.password FROM public.orders LIMIT 1) FROM public.users"
    assert SQLValidator(SecurityConfig(), blocked_columns=["users.password"]).validate(sql)[0]
