"""Testes do GitHubAuthService — sem rede real e sem tocar o ~/.gitconfig real.

A camada de rede (``_request``) e o ``git config`` subprocess são
monkeypatched; o credential store usa ``tmp_path`` como HOME.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deilebot.foundation.github_auth import (DeviceCodeGrant, GitHubAuthError,
                                             GitHubAuthService, GitHubIdentity,
                                             _classify_token, _is_github_line)


# ----- helpers puros -----

def test_classify_token_by_prefix():
    assert _classify_token("ghp_abc") == "PAT clássico"
    assert _classify_token("github_pat_abc") == "PAT fine-grained"
    assert _classify_token("gho_abc") == "token OAuth"
    assert _classify_token("ghs_abc") == "token server-to-server"
    assert _classify_token("qualquer-coisa") == "token"


def test_is_github_line():
    assert _is_github_line("https://oauth2:tok@github.com") is True
    assert _is_github_line("https://user:pw@gitlab.com") is False
    assert _is_github_line("https://oauth2:tok@github.com.evil.com") is False
    assert _is_github_line("") is False


# ----- credential store (filesystem puro) -----

def test_write_and_read_credential(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    svc._write_github_credential("ghp_TESTTOKEN123456789012")
    assert svc.credentials_path.exists()
    assert (svc.credentials_path.stat().st_mode & 0o777) == 0o600
    assert svc.stored_token() == "ghp_TESTTOKEN123456789012"
    assert "https://oauth2:ghp_TESTTOKEN123456789012@github.com" in (
        svc.credentials_path.read_text()
    )


def test_write_credential_preserves_other_hosts(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    svc.credentials_path.write_text("https://user:pw@gitlab.com\n")
    svc._write_github_credential("ghp_NEW00000000000000000000")
    lines = svc.credentials_path.read_text().splitlines()
    assert any("gitlab.com" in ln for ln in lines)
    assert any("github.com" in ln for ln in lines)
    assert svc.stored_token() == "ghp_NEW00000000000000000000"


def test_write_credential_replaces_old_github_line(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    svc._write_github_credential("ghp_OLD00000000000000000000")
    svc._write_github_credential("ghp_NEW00000000000000000000")
    github_lines = [
        ln for ln in svc.credentials_path.read_text().splitlines()
        if "github.com" in ln
    ]
    assert len(github_lines) == 1
    assert svc.stored_token() == "ghp_NEW00000000000000000000"


async def test_logout_removes_github_only(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    svc.credentials_path.write_text(
        "https://oauth2:ghp_x@github.com\nhttps://user:pw@gitlab.com\n"
    )
    svc._write_metadata(GitHubIdentity(login="octocat"), method="pat")
    removed = await svc.logout()
    assert removed is True
    lines = svc.credentials_path.read_text().splitlines()
    assert all("github.com" not in ln for ln in lines)
    assert any("gitlab.com" in ln for ln in lines)
    assert svc.stored_metadata() is None


async def test_logout_nothing_to_remove(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    assert await svc.logout() is False


async def test_current_identity_none_when_logged_out(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    assert await svc.current_identity() is None


# ----- validação de token (rede monkeypatched) -----

async def test_validate_token_success(monkeypatch, tmp_path):
    svc = GitHubAuthService(home=tmp_path)

    async def fake_request(method, url, *, headers, data=None, timeout=15.0):
        assert method == "GET"
        assert url.endswith("/user")
        assert headers["Authorization"].startswith("Bearer ")
        return 200, {"login": "octocat", "name": "The Octocat"}, {
            "X-OAuth-Scopes": "repo, read:org"
        }

    monkeypatch.setattr(svc, "_request", fake_request)
    identity = await svc.validate_token("ghp_xxxxxxxxxxxxxxxxxxxx")
    assert identity.login == "octocat"
    assert identity.name == "The Octocat"
    assert identity.scopes == "repo, read:org"
    assert identity.token_type == "PAT clássico"


async def test_validate_token_401_raises(monkeypatch, tmp_path):
    svc = GitHubAuthService(home=tmp_path)

    async def fake_request(method, url, *, headers, data=None, timeout=15.0):
        return 401, {}, {}

    monkeypatch.setattr(svc, "_request", fake_request)
    with pytest.raises(GitHubAuthError):
        await svc.validate_token("ghp_invalido")


async def test_validate_token_empty_raises(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    with pytest.raises(GitHubAuthError):
        await svc.validate_token("   ")


async def test_install_credentials(monkeypatch, tmp_path):
    svc = GitHubAuthService(home=tmp_path)

    async def fake_validate(token):
        return GitHubIdentity(
            login="octocat", name="Octo", scopes="repo", token_type="PAT clássico"
        )

    async def fake_helper():
        return None

    monkeypatch.setattr(svc, "validate_token", fake_validate)
    monkeypatch.setattr(svc, "_configure_git_helper", fake_helper)

    identity = await svc.install_credentials("ghp_TOKEN0000000000000000", method="pat")
    assert identity.login == "octocat"
    assert svc.stored_token() == "ghp_TOKEN0000000000000000"

    meta = svc.stored_metadata()
    assert meta is not None
    assert meta["login"] == "octocat"
    assert meta["method"] == "pat"
    assert "obtained_at" in meta
    # O token NUNCA pode aparecer nos metadados.
    assert "ghp_TOKEN0000000000000000" not in svc.metadata_path.read_text()


# ----- OAuth device flow (rede + sleep monkeypatched) -----

async def test_start_device_flow(monkeypatch, tmp_path):
    svc = GitHubAuthService(home=tmp_path)

    async def fake_request(method, url, *, headers, data=None, timeout=15.0):
        assert method == "POST"
        assert data["client_id"] == "Iv1.abc123"
        return 200, {
            "device_code": "DEV-123",
            "user_code": "WXYZ-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        }, {}

    monkeypatch.setattr(svc, "_request", fake_request)
    grant = await svc.start_device_flow("Iv1.abc123", "repo")
    assert grant.user_code == "WXYZ-1234"
    assert grant.device_code == "DEV-123"
    assert grant.interval == 5


async def test_start_device_flow_requires_client_id(tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    with pytest.raises(GitHubAuthError):
        await svc.start_device_flow("", "repo")


async def test_poll_device_flow_success(monkeypatch, tmp_path):
    svc = GitHubAuthService(home=tmp_path)
    calls = {"n": 0}

    async def fake_request(method, url, *, headers, data=None, timeout=15.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {"error": "authorization_pending"}, {}
        return 200, {"access_token": "gho_RESULT_TOKEN"}, {}

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(svc, "_request", fake_request)
    grant = DeviceCodeGrant(
        device_code="D", user_code="U", verification_uri="x",
        interval=1, expires_in=900,
    )
    token = await svc.poll_device_flow("Iv1.abc123", grant)
    assert token == "gho_RESULT_TOKEN"
    assert calls["n"] == 2


async def test_poll_device_flow_access_denied(monkeypatch, tmp_path):
    svc = GitHubAuthService(home=tmp_path)

    async def fake_request(method, url, *, headers, data=None, timeout=15.0):
        return 200, {"error": "access_denied"}, {}

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(svc, "_request", fake_request)
    grant = DeviceCodeGrant(device_code="D", user_code="U", verification_uri="x")
    with pytest.raises(GitHubAuthError):
        await svc.poll_device_flow("Iv1.abc123", grant)


# ----- settings -----

def test_github_settings_defaults():
    from deilebot.foundation.settings import BotSettings, GitHubSettings

    gh = GitHubSettings()
    assert gh.oauth_client_id == ""
    assert gh.oauth_scope == "repo"
    assert BotSettings().github.oauth_client_id == ""
