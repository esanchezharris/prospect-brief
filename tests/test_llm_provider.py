import pytest

from prospect_brief.config import Config
from prospect_brief.llm import make_provider, provider_name


def test_provider_switch_and_missing_keys(monkeypatch):
    cfg = Config.load()
    monkeypatch.delenv("PROSPECT_LLM", raising=False)
    assert provider_name(cfg) == "anthropic"
    cfg.data["llm"]["provider"] = "openai"
    assert provider_name(cfg) == "openai"
    monkeypatch.setenv("PROSPECT_LLM", "anthropic")
    assert provider_name(cfg) == "anthropic"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        make_provider(cfg, None, "openai")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        make_provider(cfg, None, "anthropic")
    with pytest.raises(RuntimeError, match="unknown"):
        make_provider(cfg, None, "gemini")


def test_openai_provider_maps_roles_and_prices(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-used")
    cfg = Config.load()
    p = make_provider(cfg, None, "openai")
    assert p.models == {"writer": "gpt-6-sol", "checker": "gpt-6-sol", "author": "gpt-6-astra"}
    p._account("gpt-6-sol", 1_000_000, 100_000)
    assert round(p.usage()[0].cost_usd, 2) == 3.00
