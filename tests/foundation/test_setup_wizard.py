"""Testes do SetupWizard — sem rede real e sem terminal real.

A camada de rede (``http_json``) e os prompts (``input_fn`` / ``secret_fn``)
são injetados; o filesystem usa ``tmp_path``. Nada toca o ``.env`` real.
"""

from __future__ import annotations

import json

import pytest

from deilebot.foundation.setup_wizard import (_INVITE_PERMISSION_BITS,
                                              SetupError, SetupWizard,
                                              WizardConfig, _merge_env_file,
                                              _render_deilebot_yaml)


# ----- helpers -----

def _script(*responses):
    """Callable que devolve as respostas na ordem dada (uma por chamada)."""
    items = iter(responses)
    return lambda *_a, **_kw: next(items)


async def _ok_http(method, url, headers):
    assert method == "GET"
    assert url.endswith("/users/@me")
    assert headers["Authorization"].startswith("Bot ")
    return 200, {"id": "999888777666555", "username": "TestBot"}


async def _http_401(method, url, headers):
    return 401, {}


async def _http_500(method, url, headers):
    return 500, {}


def _wizard(tmp_path, *, inputs=(), secrets_=(), http=None, tty=True):
    return SetupWizard(
        tmp_path,
        input_fn=_script(*inputs),
        secret_fn=_script(*secrets_),
        output_fn=lambda *_a, **_kw: None,
        http_json=http or _ok_http,
        isatty=lambda: tty,
    )


# ----- render do YAML local (puro) -----

def test_render_deilebot_yaml_single_owner():
    cfg = WizardConfig(owner_ids=["123456789012345678"],
                       github_oauth_client_id="Iv1.abc123")
    text = _render_deilebot_yaml(cfg)
    assert '- "discord:123456789012345678"' in text
    assert 'oauth_client_id: "Iv1.abc123"' in text
    assert "{ mode: owner_only }" in text


def test_render_deilebot_yaml_multiple_owners():
    cfg = WizardConfig(owner_ids=["111111111111111111", "222222222222222222"])
    text = _render_deilebot_yaml(cfg)
    assert '- "discord:111111111111111111"' in text
    assert '- "discord:222222222222222222"' in text


# ----- merge do .env (filesystem puro) -----

def test_merge_env_fresh(tmp_path):
    path = tmp_path / ".env"
    _merge_env_file(path, {"DEILE_BOT_DISCORD_TOKEN": "tok123"})
    assert path.is_file()
    assert "DEILE_BOT_DISCORD_TOKEN=tok123" in path.read_text()


def test_merge_env_preserves_unrelated_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comentário\nOUTRA_VAR=mantida\n")
    _merge_env_file(path, {"DEILE_BOT_DISCORD_TOKEN": "novo"})
    text = path.read_text()
    assert "# comentário" in text
    assert "OUTRA_VAR=mantida" in text
    assert "DEILE_BOT_DISCORD_TOKEN=novo" in text


def test_merge_env_replaces_existing_key_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DEILE_BOT_DISCORD_TOKEN=antigo\nOUTRA=x\n")
    _merge_env_file(path, {"DEILE_BOT_DISCORD_TOKEN": "atualizado"})
    lines = path.read_text().splitlines()
    token_lines = [ln for ln in lines if ln.startswith("DEILE_BOT_DISCORD_TOKEN=")]
    assert token_lines == ["DEILE_BOT_DISCORD_TOKEN=atualizado"]
    assert "OUTRA=x" in lines


# ----- URL de convite -----

def test_invite_url_shape(tmp_path):
    url = _wizard(tmp_path)._invite_url("42")
    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert "client_id=42" in url
    assert f"permissions={_INVITE_PERMISSION_BITS}" in url
    assert "scope=bot+applications.commands" in url


# ----- validação do token Discord (rede mockada) -----

async def test_validate_token_success(tmp_path):
    w = _wizard(tmp_path, http=_ok_http)
    app_id, name = await w._validate_discord_token("um-token-qualquer")
    assert app_id == "999888777666555"
    assert name == "TestBot"


async def test_validate_token_401_raises(tmp_path):
    w = _wizard(tmp_path, http=_http_401)
    with pytest.raises(SetupError):
        await w._validate_discord_token("token-ruim")


async def test_validate_token_non_200_raises(tmp_path):
    w = _wizard(tmp_path, http=_http_500)
    with pytest.raises(SetupError):
        await w._validate_discord_token("token")


async def test_validate_token_empty_raises(tmp_path):
    w = _wizard(tmp_path)
    with pytest.raises(SetupError):
        await w._validate_discord_token("   ")


# ----- detecção de config existente -----

def test_detect_existing_false_when_no_env(tmp_path):
    assert _wizard(tmp_path)._detect_existing() is False


def test_detect_existing_true_with_token(tmp_path):
    (tmp_path / ".env").write_text("DEILE_BOT_DISCORD_TOKEN=algo\n")
    assert _wizard(tmp_path)._detect_existing() is True


# ----- guarda de terminal não-interativo -----

async def test_non_tty_returns_config_error(tmp_path):
    w = _wizard(tmp_path, tty=False)
    assert await w.run() == 78


# ----- fluxo local completo -----

async def test_full_local_flow_writes_files(tmp_path):
    w = _wizard(
        tmp_path,
        inputs=["1", "123456789012345678", "n", "1", "8765", "", "s"],
        secrets_=["fake.discord.token", "sk-ant-test-key"],
        http=_ok_http,
    )
    rc = await w.run()
    assert rc == 0

    env_text = (tmp_path / ".env").read_text()
    assert "DEILE_BOT_DISCORD_TOKEN=fake.discord.token" in env_text
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in env_text
    assert "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN=" in env_text
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600

    yaml_text = (tmp_path / "config" / "deilebot.yaml").read_text()
    assert '- "discord:123456789012345678"' in yaml_text

    state = json.loads((tmp_path / ".deile" / "deploy.json").read_text())
    assert state == {"target": "local"}


async def test_full_local_flow_multiple_owners(tmp_path):
    w = _wizard(
        tmp_path,
        inputs=["1", "111111111111111111", "s", "222222222222222222", "n",
                "1", "8765", "", "s"],
        secrets_=["fake.token", "sk-key"],
        http=_ok_http,
    )
    assert await w.run() == 0
    yaml_text = (tmp_path / "config" / "deilebot.yaml").read_text()
    assert '- "discord:111111111111111111"' in yaml_text
    assert '- "discord:222222222222222222"' in yaml_text


async def test_full_local_flow_cancelled_writes_nothing(tmp_path):
    w = _wizard(
        tmp_path,
        inputs=["1", "123456789012345678", "n", "1", "8765", "", "n"],
        secrets_=["fake.discord.token", "sk-ant-test-key"],
        http=_ok_http,
    )
    assert await w.run() == 1
    assert not (tmp_path / ".env").exists()


# ----- patch do ConfigMap do bot (modo container) -----

_SAMPLE_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: bot-config
data:
  deilebot.yaml: |
    permissions:
      owners:
        - "discord:1475913578648436909"
    github:
      oauth_client_id: ""
      oauth_scope: "repo"
"""


def test_patch_bot_configmap_multiple_owners(tmp_path):
    cm = tmp_path / "infra" / "k8s" / "manifests" / "15-bot-config.yaml"
    cm.parent.mkdir(parents=True)
    cm.write_text(_SAMPLE_CONFIGMAP)

    w = _wizard(tmp_path)
    w._patch_bot_configmap(WizardConfig(
        owner_ids=["555000111222333", "666000111222333"],
        github_oauth_client_id="Iv1.xyz",
    ))

    text = cm.read_text()
    assert '- "discord:555000111222333"' in text
    assert '- "discord:666000111222333"' in text
    assert '- "discord:1475913578648436909"' not in text
    assert 'oauth_client_id: "Iv1.xyz"' in text


def test_patch_bot_configmap_missing_file_raises(tmp_path):
    with pytest.raises(SetupError):
        _wizard(tmp_path)._patch_bot_configmap(WizardConfig(owner_ids=["1"]))


# ----- modo container: declinar o deploy grava config sem aplicar -----

async def test_container_decline_deploy_writes_config(tmp_path):
    deploy = tmp_path / "infra" / "k8s" / "deploy.py"
    deploy.parent.mkdir(parents=True)
    deploy.write_text("# stub\n")
    cm = tmp_path / "infra" / "k8s" / "manifests" / "15-bot-config.yaml"
    cm.parent.mkdir(parents=True)
    cm.write_text(_SAMPLE_CONFIGMAP)

    w = _wizard(tmp_path, inputs=["n"])  # "n" para "Buildar e fazer o deploy?"
    cfg = WizardConfig(
        mode="container",
        discord_token="tok",
        owner_ids=["123456789012345678"],
        llm_provider="openai",
        llm_key="sk-openai",
    )
    rc = await w._apply_container(cfg)
    assert rc == 0
    assert "DEILE_BOT_DISCORD_TOKEN=tok" in (tmp_path / ".env").read_text()
    assert '- "discord:123456789012345678"' in cm.read_text()
    state = json.loads((tmp_path / ".deile" / "deploy.json").read_text())
    assert state == {"target": "container"}


async def test_apply_container_without_deploy_script_raises(tmp_path):
    cm = tmp_path / "infra" / "k8s" / "manifests" / "15-bot-config.yaml"
    cm.parent.mkdir(parents=True)
    cm.write_text(_SAMPLE_CONFIGMAP)
    w = _wizard(tmp_path)
    with pytest.raises(SetupError):
        await w._apply_container(WizardConfig(mode="container",
                                              owner_ids=["123456789012345678"],
                                              llm_provider="openai", llm_key="k"))
