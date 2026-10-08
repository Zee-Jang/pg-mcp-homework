"""Custom gateway routing for both model-backed services."""

from pathlib import Path

import pytest

from pg_mcp.config.settings import OpenAIConfig, Settings, ValidationConfig
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_generator import SQLGenerator


def test_model_endpoint_loads_from_dotenv(tmp_path: Path) -> None:
    config_file = tmp_path / ".env"
    config_file.write_text(
        "OPENAI_API_KEY=sk-test\n"
        "OPENAI_MODEL=local-model\n"
        "OPENAI_BASE_URL=http://127.0.0.1:8999/v1\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=config_file)
    assert settings.openai.base_url == "http://127.0.0.1:8999/v1"
    assert settings.openai.model == "local-model"


@pytest.mark.parametrize("service", ["generation", "validation"])
async def test_both_services_route_to_configured_gateway(service: str) -> None:
    config = OpenAIConfig(api_key="sk-test", base_url="http://127.0.0.1:8999/v1")
    component = (
        SQLGenerator(config)
        if service == "generation"
        else ResultValidator(config, ValidationConfig())
    )
    try:
        assert str(component.client.base_url) == "http://127.0.0.1:8999/v1/"
    finally:
        await component.client.close()
