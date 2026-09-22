import pytest

from prospect_brief.config import Config


def make_config(root):
    cfg = Config.load()
    cfg.root = root  # cache/, runs/, briefs/ go under tmp
    cfg.data["fetch"]["default_domain_rps"] = 200  # fixture transport, no real host to protect
    cfg.data["fetch"]["domain_rps"] = {}
    cfg.data["institution_domains"]["University of Southern California"] = "fixture.example"
    return cfg


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)
