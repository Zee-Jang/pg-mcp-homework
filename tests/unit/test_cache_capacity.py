"""Configured cache capacity and recency must affect retained schemas."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import CacheConfig
from pg_mcp.models.schema import DatabaseSchema


@pytest.fixture
def cache() -> SchemaCache:
    return SchemaCache(CacheConfig(max_size=2))


@pytest.fixture
def introspection():
    def make_introspector(pool, database_name):
        return MagicMock(
            introspect=AsyncMock(return_value=DatabaseSchema(database_name=database_name))
        )

    with patch(
        "pg_mcp.cache.schema_cache.SchemaIntrospector", side_effect=make_introspector
    ):
        yield


@pytest.mark.usefixtures("introspection")
async def test_loading_third_database_evicts_oldest_schema(cache: SchemaCache) -> None:
    pool = MagicMock()
    for name in ("first", "second", "third"):
        await cache.load(name, pool)
    assert cache.get("first") is None
    assert set(cache.get_cached_databases()) == {"second", "third"}
    assert cache.get_cache_age("first") is None


@pytest.mark.usefixtures("introspection")
async def test_cache_hit_keeps_active_database_when_capacity_is_reached(cache: SchemaCache) -> None:
    pool = MagicMock()
    first = await cache.load("first", pool)
    await cache.load("second", pool)
    assert cache.get("first") is first
    await cache.load("third", pool)
    assert cache.get("second") is None
    assert cache.get("first") is first
    assert set(cache.get_cached_databases()) == {"first", "third"}


@pytest.mark.usefixtures("introspection")
async def test_refresh_at_capacity_preserves_other_database(cache: SchemaCache) -> None:
    pool = MagicMock()
    await cache.load("first", pool)
    await cache.load("second", pool)
    await cache.refresh("first", pool)
    assert set(cache.get_cached_databases()) == {"first", "second"}
    await cache.load("third", pool)
    assert cache.get("second") is None
    assert set(cache.get_cached_databases()) == {"first", "third"}
