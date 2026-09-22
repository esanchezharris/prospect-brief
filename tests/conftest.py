import pytest

from prospect_brief.config import Config


@pytest.fixture
def config(tmp_path):
    cfg = Config.load()
    cfg.root = tmp_path  # cache/, runs/, briefs/ go under tmp
    cfg.data["fetch"]["default_domain_rps"] = 200  # fixture transport, no real host to protect
    return cfg
