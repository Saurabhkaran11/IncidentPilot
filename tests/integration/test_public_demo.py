"""The public demo must stay inside the simulation, and local mode must stay local."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.domain.enums import AgentMode, Mode
from packages.domain.errors import ApiError
from services.api import auth, config, main


def settings(**extra):
    base = dict(mode=Mode.FIXTURE, agent_mode=AgentMode.STUB, data_dir=Path("data"), ui_origin="http://x",
                workspace_id="w", app_id="a", dev_actor_id="dev")
    return config.Settings(**{**base, **extra})


def request(host):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={})


def test_outside_callers_are_refused_unless_public_demo_is_on():
    with pytest.raises(ApiError):
        auth.authenticate(request("203.0.113.9"), None, settings())
    principal = auth.authenticate(request("203.0.113.9"), None, settings(public_demo=True))
    assert principal.actor_id == "public-demo-visitor" and principal.role == "operator"


@pytest.mark.parametrize("env", [{"INCIDENTPILOT_MODE": "aws_live"}, {"INCIDENTPILOT_AGENT_MODE": "bedrock"}])
def test_public_demo_refuses_to_start_with_real_aws_or_a_real_model(monkeypatch, env):
    monkeypatch.setenv("INCIDENTPILOT_PUBLIC_DEMO", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            config.get_settings()
    finally:
        config.get_settings.cache_clear()


def test_public_demo_is_off_by_default(monkeypatch):
    monkeypatch.delenv("INCIDENTPILOT_PUBLIC_DEMO", raising=False)
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().public_demo is False
    finally:
        config.get_settings.cache_clear()


def test_rate_limit_applies_per_visitor():
    main._public_posts.clear()
    for _ in range(main.PUBLIC_POSTS_PER_MINUTE):
        assert main._public_rate_limited("a", 100.0) is False
    assert main._public_rate_limited("a", 100.0) is True
    assert main._public_rate_limited("b", 100.0) is False
    assert main._public_rate_limited("a", 161.0) is False
