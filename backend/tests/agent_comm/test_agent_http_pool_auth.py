"""AgentHttpPool carries optional httpx.BasicAuth, exposed as .auth attribute."""

from __future__ import annotations

import httpx2 as httpx

from app.agent_comm.config import AgentCommConfig
from app.agent_comm.http_pool import AgentHttpPool, build_agent_basic_auth


def test_pool_default_auth_is_none() -> None:
    pool = AgentHttpPool()
    assert pool.auth is None


def test_pool_constructed_with_auth_exposes_it() -> None:
    auth = httpx.BasicAuth("alice", "s3cret")
    pool = AgentHttpPool(agent_auth=auth)
    assert pool.auth is auth


def test_build_agent_basic_auth_returns_none_when_creds_missing() -> None:
    settings = AgentCommConfig(agent_auth_username=None, agent_auth_password=None)
    assert build_agent_basic_auth(settings) is None


def test_build_agent_basic_auth_returns_basicauth_when_creds_present() -> None:
    settings = AgentCommConfig(agent_auth_username="alice", agent_auth_password="s3cret")
    result = build_agent_basic_auth(settings)
    assert isinstance(result, httpx.BasicAuth)
