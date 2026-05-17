"""github_auth — autenticação no GitHub para as operações git do bot.

Dois métodos de login, à escolha do operador (ver o cog ``/github_login``):

  * PAT — o operador cola um Personal Access Token. Funciona sem
    configuração nenhuma. Aceita tanto tokens clássicos (``ghp_``)
    quanto fine-grained (``github_pat_``).
  * OAuth device flow (RFC 8628) — exige um GitHub OAuth App registrado
    (o Client ID *público* em ``github.oauth_client_id``). O bot pede ao
    GitHub um ``user_code``, o operador digita esse código em
    github.com/login/device e o bot faz polling até o GitHub devolver
    um access token.

Qualquer que seja o método, o token resultante é instalado do MESMO
jeito que ``wrapper.py:_setup_git_credentials()`` instala o token de
boot: uma única linha ``https://oauth2:<token>@github.com`` em
``~/.git-credentials`` (modo 0600) mais ``git config --global
credential.helper store``. O token vive APENAS nesse arquivo 0600 —
nunca em argv, nunca em variável de ambiente, nunca no conversation
store. Metadados não-sensíveis (login, método, timestamp) vão para
``~/.deile/github_auth.json`` para o ``/github_status`` reportar sem
precisar reler o token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

_logger = logging.getLogger("deilebot.github_auth")

# RFC 8628 — OAuth 2.0 Device Authorization Grant.
_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_API_BASE = "https://api.github.com"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_AGENT = "deilebot-github-auth"


class GitHubAuthError(Exception):
    """Falha de um fluxo de login GitHub — a mensagem é pt-BR, vai para o usuário."""


@dataclass(frozen=True)
class GitHubIdentity:
    """Identidade resolvida a partir de um token válido."""

    login: str
    name: str = ""
    scopes: str = ""
    token_type: str = "token"


@dataclass(frozen=True)
class DeviceCodeGrant:
    """Resposta do GitHub ao iniciar o device flow."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int = 5
    expires_in: int = 900


def _classify_token(token: str) -> str:
    """Classifica o token pelo prefixo (apenas para exibição)."""
    if token.startswith("github_pat_"):
        return "PAT fine-grained"
    if token.startswith("ghp_"):
        return "PAT clássico"
    if token.startswith("gho_"):
        return "token OAuth"
    if token.startswith("ghs_"):
        return "token server-to-server"
    return "token"


def _is_github_line(line: str) -> bool:
    """True se uma linha do ``~/.git-credentials`` aponta para github.com."""
    line = line.strip()
    if not line:
        return False
    try:
        return (urlsplit(line).hostname or "").lower() == "github.com"
    except ValueError:
        return False


class GitHubAuthService:
    """Valida tokens GitHub, roda o device flow e gerencia a credencial git.

    ``home`` é injetável para permitir testes isolados; em produção cai
    no ``$HOME`` do processo (``/home/deile`` no container).
    """

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        api_base: str = _API_BASE,
        device_code_url: str = _DEVICE_CODE_URL,
        access_token_url: str = _ACCESS_TOKEN_URL,
    ) -> None:
        self._home = Path(home) if home else Path(os.environ.get("HOME", "/home/deile"))
        self._api_base = api_base.rstrip("/")
        self._device_code_url = device_code_url
        self._access_token_url = access_token_url

    # ----- caminhos -----
    @property
    def credentials_path(self) -> Path:
        return self._home / ".git-credentials"

    @property
    def metadata_path(self) -> Path:
        return self._home / ".deile" / "github_auth.json"

    # ----- ponto único de I/O de rede (monkeypatched nos testes) -----
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        data: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """Único ponto de saída HTTP. Devolve ``(status, json_body, headers)``."""
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.request(method, url, headers=headers, data=data) as resp:
                    resp_headers = {k: v for k, v in resp.headers.items()}
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                    return resp.status, body, resp_headers
        except aiohttp.ClientError as exc:
            raise GitHubAuthError(f"falha de rede ao contatar o GitHub: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise GitHubAuthError("tempo esgotado ao contatar o GitHub") from exc

    # ----- validação de token -----
    async def validate_token(self, token: str) -> GitHubIdentity:
        """``GET /user`` com o token. Levanta ``GitHubAuthError`` se inválido."""
        token = (token or "").strip()
        if not token:
            raise GitHubAuthError("token vazio")
        status, body, headers = await self._request(
            "GET",
            f"{self._api_base}/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": _USER_AGENT,
            },
        )
        if status == 401:
            raise GitHubAuthError("token inválido ou expirado (HTTP 401 do GitHub)")
        if status != 200:
            raise GitHubAuthError(
                f"o GitHub recusou o token (HTTP {status}) — "
                "verifique se ele tem os escopos necessários"
            )
        login = str(body.get("login") or "").strip()
        if not login:
            raise GitHubAuthError("resposta do GitHub sem o campo 'login'")
        return GitHubIdentity(
            login=login,
            name=str(body.get("name") or ""),
            scopes=str(headers.get("X-OAuth-Scopes") or "").strip(),
            token_type=_classify_token(token),
        )

    # ----- instalação da credencial -----
    async def install_credentials(self, token: str, *, method: str) -> GitHubIdentity:
        """Valida o token e instala no credential store do git."""
        identity = await self.validate_token(token)
        self._write_github_credential(token.strip())
        await self._configure_git_helper()
        self._write_metadata(identity, method=method)
        _logger.info(
            "github_auth: credencial instalada para login=%s método=%s",
            identity.login, method,
        )
        return identity

    def _write_github_credential(self, token: str) -> None:
        """Substitui a linha github.com no ``~/.git-credentials`` (0600, atômico)."""
        lines = self._read_credential_lines()
        kept = [ln for ln in lines if not _is_github_line(ln)]
        new_line = f"https://oauth2:{token}@github.com"
        content = "\n".join([new_line, *kept]).strip() + "\n"
        self._atomic_write(self.credentials_path, content)

    def _read_credential_lines(self) -> List[str]:
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            _logger.warning("github_auth: não consegui ler ~/.git-credentials: %s", exc)
            return []
        return [ln for ln in raw.splitlines() if ln.strip()]

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Cria/sobrescreve ``path`` no modo 0600 sem janela TOCTOU."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    async def _configure_git_helper(self) -> None:
        """``git config --global credential.helper store`` (idempotente)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "config", "--global", "credential.helper", "store",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                _logger.warning(
                    "github_auth: 'git config credential.helper' saiu %s: %s",
                    proc.returncode, stderr.decode("utf-8", "replace")[:200],
                )
        except FileNotFoundError:
            _logger.warning("github_auth: git não encontrado — credential.helper não configurado")
        except OSError as exc:
            _logger.warning("github_auth: não consegui rodar git config: %s", exc)

    # ----- metadados -----
    def _write_metadata(self, identity: GitHubIdentity, *, method: str) -> None:
        """Grava só metadados NÃO-sensíveis (sem o token)."""
        meta = {
            "login": identity.login,
            "name": identity.name,
            "method": method,
            "scopes": identity.scopes,
            "token_type": identity.token_type,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write(self.metadata_path, json.dumps(meta, indent=2) + "\n")

    def stored_metadata(self) -> Optional[Dict[str, Any]]:
        """Lê os metadados persistidos, ou ``None`` se ausentes/corrompidos."""
        try:
            raw = self.metadata_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def stored_token(self) -> Optional[str]:
        """Extrai o token github.com do ``~/.git-credentials``, se houver."""
        for line in self._read_credential_lines():
            if _is_github_line(line):
                parts = urlsplit(line.strip())
                if parts.password:
                    return parts.password
        return None

    async def current_identity(self) -> Optional[GitHubIdentity]:
        """Revalida o token persistido ao vivo. ``None`` se ausente/inválido."""
        token = self.stored_token()
        if not token:
            return None
        try:
            return await self.validate_token(token)
        except GitHubAuthError:
            return None

    async def logout(self) -> bool:
        """Remove a credencial github.com + metadados. True se removeu algo."""
        removed = False
        lines = self._read_credential_lines()
        kept = [ln for ln in lines if not _is_github_line(ln)]
        if len(kept) != len(lines):
            removed = True
            if kept:
                self._atomic_write(self.credentials_path, "\n".join(kept) + "\n")
            else:
                try:
                    self.credentials_path.unlink()
                except OSError:
                    pass
        try:
            self.metadata_path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.warning("github_auth: não consegui remover metadados: %s", exc)
        return removed

    # ----- OAuth device flow (RFC 8628) -----
    async def start_device_flow(self, client_id: str, scope: str) -> DeviceCodeGrant:
        """Pede ao GitHub um ``user_code``/``device_code``."""
        if not client_id:
            raise GitHubAuthError(
                "OAuth não configurado — o operador precisa registrar um GitHub "
                "OAuth App e definir `github.oauth_client_id` no deilebot.yaml. "
                "Use o método PAT enquanto isso."
            )
        status, body, _ = await self._request(
            "POST",
            self._device_code_url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            data={"client_id": client_id, "scope": scope},
        )
        if status != 200 or "device_code" not in body:
            err = body.get("error_description") or body.get("error") or f"HTTP {status}"
            raise GitHubAuthError(f"o GitHub recusou iniciar o device flow: {err}")
        return DeviceCodeGrant(
            device_code=str(body["device_code"]),
            user_code=str(body.get("user_code", "")),
            verification_uri=str(
                body.get("verification_uri", "https://github.com/login/device")
            ),
            interval=int(body.get("interval", 5) or 5),
            expires_in=int(body.get("expires_in", 900) or 900),
        )

    async def poll_device_flow(self, client_id: str, grant: DeviceCodeGrant) -> str:
        """Faz polling até o operador autorizar. Devolve o access token."""
        interval = max(grant.interval, 1)
        deadline = time.monotonic() + grant.expires_in
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            status, body, _ = await self._request(
                "POST",
                self._access_token_url,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
                data={
                    "client_id": client_id,
                    "device_code": grant.device_code,
                    "grant_type": _DEVICE_GRANT_TYPE,
                },
            )
            token = body.get("access_token")
            if token:
                return str(token)
            error = str(body.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                # RFC 8628 §3.5: ao receber slow_down, aumentar o intervalo em 5s.
                interval += 5
                continue
            if error == "expired_token":
                break
            if error == "access_denied":
                raise GitHubAuthError("autorização negada no GitHub")
            detail = body.get("error_description") or error or f"HTTP {status}"
            raise GitHubAuthError(f"o GitHub recusou o device flow: {detail}")
        raise GitHubAuthError(
            "o código expirou antes da autorização — refaça o /github_login"
        )
