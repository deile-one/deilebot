"""Assistente de configuração interativa do deilebot.

Um clone novo não tem `.env` nem `config/deilebot.yaml` — nada diz qual
bot do Discord o processo controla. Este wizard preenche essa lacuna:
pergunta o que só um humano pode fornecer (token do bot, IDs dos donos,
chave de LLM), valida o token ao vivo na API do Discord, grava os
arquivos de configuração e — no modo container — dispara o build e o
deploy via o orquestrador `infra/k8s/deploy.py`.

Dois modos:
  local      — grava `.env` + `config/deilebot.yaml`; opcionalmente sobe
               o bot como serviço (via `deploy.py start`).
  container  — grava `.env`, ajusta o ConfigMap do k8s com os donos, e
               builda + aplica a stack via `deploy.py`.

O wizard não cria a aplicação no Discord (o Discord não expõe API para
isso) — ele imprime o passo-a-passo e valida o token colado. Também não
instala o Kubernetes; quem cuida disso é o `deploy.py` (que chama o
`setup_environment.py` quando preciso).

Entrada: `run_setup()`, ligado em `deilebot.cli` como o subcomando
`setup` e oferecido automaticamente pelo `run` quando não há token.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_DISCORD_API = "https://discord.com/api/v10"

# Provedor de LLM → a env var de onde o DEILE lê a chave.
_LLM_PROVIDERS: Dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_API_KEY",
}

# Bits de permissão que o bot precisa (enviar/editar mensagens, reagir,
# thread, fixar, mencionar). Somados na URL de convite — nada além disso.
_INVITE_PERMISSION_BITS = (
    (1 << 10)    # View Channels
    | (1 << 11)  # Send Messages
    | (1 << 13)  # Manage Messages (fixar)
    | (1 << 14)  # Embed Links
    | (1 << 15)  # Attach Files
    | (1 << 6)   # Add Reactions
    | (1 << 16)  # Read Message History
    | (1 << 17)  # Mention Everyone
    | (1 << 35)  # Create Public Threads
    | (1 << 38)  # Send Messages in Threads
)


class SetupError(RuntimeError):
    """Erro irreparável do wizard (token ruim, infra ausente, etc.)."""


@dataclass
class WizardConfig:
    """Tudo que o wizard coleta antes de gravar qualquer coisa."""

    mode: str = "local"                       # "local" | "container"
    discord_token: str = ""
    discord_app_id: str = ""                  # derivado da validação
    discord_bot_name: str = ""                # derivado da validação
    owner_ids: List[str] = field(default_factory=list)
    llm_provider: str = ""                    # uma chave de _LLM_PROVIDERS
    llm_key: str = ""
    control_plane_token: str = field(
        default_factory=lambda: secrets.token_urlsafe(32)
    )
    control_plane_port: int = 8765
    github_oauth_client_id: str = ""
    github_token: str = ""                    # opcional, p/ o `deploy.py clone`


# ---- helpers de módulo (puros / testáveis) ----------------------------------


async def _default_http_json(
    method: str, url: str, headers: Dict[str, str]
) -> Tuple[int, dict]:
    """Único ponto de rede — monkeypatchado nos testes.

    `aiohttp` vem das dependências base do deilebot.
    """
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers) as resp:
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001 — corpo não-JSON vira {}
                body = {}
            return resp.status, body


def _merge_env_file(path: Path, updates: Dict[str, str]) -> None:
    """Grava `updates` num `.env`, trocando chaves existentes no lugar e
    anexando o resto. Linhas que o wizard não controla são preservadas.

    Rejeita valores com `\\n`/`\\r`: um caractere de quebra de linha no
    meio de um valor (paste malformado de chave/token) injetaria uma
    linha extra no `.env`, interpretada pelo python-dotenv como nova
    variável.
    """
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise SetupError(
                f"valor de {key} contém uma quebra de linha — cole o "
                "valor inteiro, sem espaços ou linhas extras"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    out: List[str] = []
    for line in existing:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- gravado pelo `deilebot setup` ---")
        out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _render_deilebot_yaml(cfg: WizardConfig) -> str:
    """Renderiza a config não-secreta do runtime (modo local)."""
    owners = "\n".join(f'    - "discord:{o}"' for o in cfg.owner_ids)
    return f"""\
# config/deilebot.yaml — gerado por `deilebot setup`.
# Estrutura não-secreta do runtime. Os segredos ficam no `.env`.
foundation:
  default_persona: developer
  intent_classifier: heuristic
  agent_bridge_mode: in_process

permissions:
  # `<provider>:<provider_user_id>` — forma estável entre deployments.
  owners:
{owners}
  allowlist_invoke_agent: ["*"]
  blocklist: []
  per_action:
    EXECUTE_TOOL:  {{ mode: owner_only }}
    SEND_DM:       {{ mode: owner_only }}
    ADMIN_COMMAND: {{ mode: owner_only }}
    DEBUG_COMMAND: {{ mode: owner_only }}

personas:
  default: developer

providers:
  enabled_providers: ["discord"]

# Login GitHub via Discord (/github_login). O método PAT funciona sem
# nada aqui. O OAuth device flow precisa do Client ID de um GitHub OAuth
# App (o Client ID é público, não é segredo).
github:
  oauth_client_id: "{cfg.github_oauth_client_id}"
  oauth_scope: "repo"
"""


# ---- o wizard ---------------------------------------------------------------


class SetupWizard:
    """Conduz a configuração interativa. Toda I/O é injetável para testes."""

    def __init__(
        self,
        root: Path,
        *,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        output_fn: Callable[..., None] = print,
        http_json: Optional[Callable] = None,
        isatty: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.root = Path(root)
        self._input = input_fn
        self._secret = secret_fn
        self._out = output_fn
        self._http_json = http_json or _default_http_json
        self._isatty = isatty or sys.stdin.isatty

    # ----- caminhos -----

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def local_yaml_path(self) -> Path:
        return self.root / "config" / "deilebot.yaml"

    @property
    def deploy_script_path(self) -> Path:
        return self.root / "infra" / "k8s" / "deploy.py"

    @property
    def bot_configmap_path(self) -> Path:
        return self.root / "infra" / "k8s" / "manifests" / "15-bot-config.yaml"

    @property
    def deploy_state_path(self) -> Path:
        return self.root / ".deile" / "deploy.json"

    # ----- orquestração -----

    async def run(self, mode: Optional[str] = None, reconfigure: bool = False) -> int:
        if not self._isatty():
            self._out(
                "deilebot setup: terminal não interativo — não dá para "
                "perguntar nada aqui.\n"
                "Configure manualmente (veja o README, seção 'Configuração "
                "manual') ou rode `deilebot setup` num terminal de verdade."
            )
            return 78  # EX_CONFIG

        self._banner()

        if self._detect_existing() and not reconfigure:
            self._out(f"\nJá existe configuração em {self.env_path}.")
            if not self._confirm("Reconfigurar do zero?", default=False):
                self._out("Nada alterado. Saindo.")
                return 0

        cfg = WizardConfig()
        cfg.mode = mode if mode in ("local", "container") else self._ask_mode()

        await self._step_discord_token(cfg)
        self._step_owners(cfg)
        self._step_llm(cfg)
        self._step_control_plane(cfg)
        self._step_github(cfg)

        self._print_summary(cfg)
        if not self._confirm("Gravar a configuração?", default=True):
            self._out("Cancelado. Nada alterado.")
            return 1

        if cfg.mode == "local":
            return await self._apply_local(cfg)
        return await self._apply_container(cfg)

    # ----- primitivas de prompt -----

    def _banner(self) -> None:
        self._out(
            "\n"
            "==================================================\n"
            "  deilebot — configuração interativa\n"
            "==================================================\n"
        )

    def _prompt(self, question: str, *, default: Optional[str] = None) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            ans = self._input(f"{question}{suffix}: ").strip()
            if ans:
                return ans
            if default is not None:
                return default
            self._out("  Resposta obrigatória.")

    def _prompt_secret(self, question: str) -> str:
        while True:
            ans = self._secret(f"{question}: ").strip()
            if ans:
                return ans
            self._out("  Resposta obrigatória.")

    def _confirm(self, question: str, *, default: bool = True) -> bool:
        hint = "[S/n]" if default else "[s/N]"
        ans = self._input(f"{question} {hint}: ").strip().lower()
        if not ans:
            return default
        return ans in ("s", "sim", "y", "yes")

    def _ask_mode(self) -> str:
        self._out(
            "Como você quer rodar o deilebot?\n"
            "  1) local      — direto no host (Python + venv)\n"
            "  2) container  — em Kubernetes (Rancher Desktop / k3s / colima)\n"
        )
        while True:
            choice = self._input("Escolha [1-2]: ").strip().lower()
            if choice in ("1", "local"):
                return "local"
            if choice in ("2", "container"):
                return "container"
            self._out("  Opção inválida.")

    # ----- etapas -----

    async def _step_discord_token(self, cfg: WizardConfig) -> None:
        self._out(
            "\n--- 1/5 · Bot do Discord ---------------------------\n"
            "Crie (ou reaproveite) um bot no Discord Developer Portal:\n"
            "  1. Abra https://discord.com/developers/applications\n"
            "  2. 'New Application' -> dê um nome -> 'Create'.\n"
            "  3. Menu lateral 'Bot'.\n"
            "  4. Em 'Privileged Gateway Intents', LIGUE:\n"
            "       - MESSAGE CONTENT INTENT\n"
            "       - SERVER MEMBERS INTENT\n"
            "  5. Clique 'Reset Token' e copie o token.\n"
        )
        while True:
            token = self._prompt_secret("Cole o token do bot")
            self._out("  Validando o token na API do Discord...")
            try:
                app_id, bot_name = await self._validate_discord_token(token)
            except SetupError as exc:
                self._out(f"  [x] {exc}")
                if not self._confirm("Tentar de novo?", default=True):
                    raise
                continue
            cfg.discord_token = token
            cfg.discord_app_id = app_id
            cfg.discord_bot_name = bot_name
            self._out(f"  [ok] Token válido — bot '{bot_name}' (app id {app_id}).")
            self._out(
                "\n  Adicione o bot ao seu servidor com este link:\n"
                f"  {self._invite_url(app_id)}\n"
            )
            return

    async def _validate_discord_token(self, token: str) -> Tuple[str, str]:
        """Bate em GET /users/@me com o token do bot; devolve (app_id, nome)."""
        token = token.strip()
        if not token:
            raise SetupError("token vazio")
        status, body = await self._http_json(
            "GET",
            f"{_DISCORD_API}/users/@me",
            {"Authorization": f"Bot {token}"},
        )
        if status == 401:
            raise SetupError(
                "token rejeitado pelo Discord (401) — confira se copiou o "
                "token inteiro e se ele não foi resetado depois"
            )
        if status != 200:
            raise SetupError(f"resposta inesperada do Discord (HTTP {status})")
        app_id = str(body.get("id") or "")
        bot_name = str(body.get("username") or "?")
        if not app_id:
            raise SetupError(
                "resposta do Discord sem 'id' — esse token não parece ser "
                "de um bot"
            )
        return app_id, bot_name

    def _invite_url(self, app_id: str) -> str:
        return (
            "https://discord.com/oauth2/authorize"
            f"?client_id={app_id}"
            f"&permissions={_INVITE_PERMISSION_BITS}"
            "&scope=bot+applications.commands"
        )

    def _step_owners(self, cfg: WizardConfig) -> None:
        self._out(
            "\n--- 2/5 · Donos do bot -----------------------------\n"
            "Os 'donos' podem usar os comandos owner-only (/github_login,\n"
            "/dlq, /audit, etc.). Para achar seu Discord User ID:\n"
            "  1. Discord -> Configurações -> Avançado -> ative 'Modo desenvolvedor'.\n"
            "  2. Botão direito no seu nome -> 'Copiar ID do usuário'.\n"
            "Você pode cadastrar mais de um dono.\n"
        )
        while True:
            digits = self._prompt("Discord User ID de um dono (só números)").strip()
            if not (digits.isdigit() and len(digits) >= 15):
                self._out(
                    "  [x] Um Discord User ID é uma sequência de 17-20 dígitos."
                )
                continue
            if digits in cfg.owner_ids:
                self._out("  · esse ID já está na lista")
            else:
                cfg.owner_ids.append(digits)
                self._out(f"  [ok] dono adicionado: discord:{digits}")
            if not self._confirm("Adicionar outro dono?", default=False):
                return

    def _step_llm(self, cfg: WizardConfig) -> None:
        self._out(
            "\n--- 3/5 · Provedor de LLM --------------------------\n"
            "O agente DEILE embarcado precisa de uma chave de API de LLM.\n"
        )
        names = list(_LLM_PROVIDERS)
        for i, name in enumerate(names, 1):
            self._out(f"  {i}) {name}  ({_LLM_PROVIDERS[name]})")
        while True:
            choice = self._input(f"Escolha [1-{len(names)}]: ").strip().lower()
            idx: Optional[int] = None
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                idx = int(choice) - 1
            elif choice in _LLM_PROVIDERS:
                idx = names.index(choice)
            if idx is not None:
                cfg.llm_provider = names[idx]
                break
            self._out("  Opção inválida.")
        cfg.llm_key = self._prompt_secret(
            f"Cole a chave {_LLM_PROVIDERS[cfg.llm_provider]}"
        )
        self._out(
            "  (a chave não é validada agora — se estiver errada o agente "
            "avisa no startup)"
        )

    def _step_control_plane(self, cfg: WizardConfig) -> None:
        self._out(
            "\n--- 4/5 · Control plane HTTP -----------------------\n"
            "É a porta pela qual o agente DEILE fala de volta com o bot\n"
            "(enviar DM, etc.). O token de auth é gerado automaticamente.\n"
        )
        if cfg.mode == "local":
            while True:
                raw = self._prompt("Porta do control plane", default="8765")
                if raw.isdigit() and 1 <= int(raw) <= 65535:
                    cfg.control_plane_port = int(raw)
                    break
                self._out(
                    "  [x] A porta precisa ser um número entre 1 e 65535."
                )
        else:
            cfg.control_plane_port = 8765  # fixo no manifesto K8s
        self._out(
            f"  [ok] token de control plane gerado "
            f"({len(cfg.control_plane_token)} chars)."
        )

    def _step_github(self, cfg: WizardConfig) -> None:
        self._out(
            "\n--- 5/5 · GitHub (opcional) ------------------------\n"
            "O /github_login tem o método PAT, que funciona sem nada aqui.\n"
            "• OAuth device flow: precisa do Client ID de um GitHub OAuth App.\n"
            "• Modo container: um GITHUB_TOKEN habilita o `deploy.py clone`.\n"
        )
        cfg.github_oauth_client_id = self._prompt(
            "GitHub OAuth App Client ID (Enter para pular)", default=""
        ).strip()
        if cfg.mode == "container" and self._confirm(
            "Definir um GITHUB_TOKEN para clonar repositórios?", default=False
        ):
            cfg.github_token = self._prompt_secret(
                "GITHUB_TOKEN (token de leitura/clone do GitHub)"
            )

    # ----- resumo + aplicação -----

    def _print_summary(self, cfg: WizardConfig) -> None:
        owners = ", ".join(f"discord:{o}" for o in cfg.owner_ids)
        self._out(
            "\n--- Resumo -----------------------------------------\n"
            f"  Modo .............. {cfg.mode}\n"
            f"  Bot do Discord .... {cfg.discord_bot_name} "
            f"(app {cfg.discord_app_id})\n"
            f"  Donos ............. {owners}\n"
            f"  LLM ............... {cfg.llm_provider}\n"
            f"  Control plane ..... porta {cfg.control_plane_port}\n"
            f"  GitHub OAuth ...... "
            f"{cfg.github_oauth_client_id or '(pulado — só PAT)'}\n"
            f"  GITHUB_TOKEN ...... "
            f"{'definido' if cfg.github_token else '(não definido)'}\n"
        )

    def _write_env(self, cfg: WizardConfig) -> None:
        updates = {
            "DEILE_BOT_DISCORD_TOKEN": cfg.discord_token,
            "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN": cfg.control_plane_token,
            # Espelho: o control plane local lê
            # DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN; o deploy.py lê
            # DEILE_BOT_AUTH_TOKEN para gerar o Secret do cluster. O mesmo
            # valor sob os dois nomes cobre local e container.
            "DEILE_BOT_AUTH_TOKEN": cfg.control_plane_token,
            _LLM_PROVIDERS[cfg.llm_provider]: cfg.llm_key,
        }
        if cfg.github_token:
            updates["GITHUB_TOKEN"] = cfg.github_token
        _merge_env_file(self.env_path, updates)
        try:
            self.env_path.chmod(0o600)
        except OSError:
            pass

    def _write_deploy_state(self, target: str) -> None:
        """Grava o alvo escolhido para o `deploy.py` saber o modo."""
        self.deploy_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.deploy_state_path.write_text(
            json.dumps({"target": target}, indent=2) + "\n", encoding="utf-8"
        )

    async def _apply_local(self, cfg: WizardConfig) -> int:
        # Ordem deliberada: arquivos sem segredo primeiro, `.env` (token
        # do Discord + chave de LLM) por ÚLTIMO — se um passo anterior
        # falhar, nenhum segredo terá sido escrito em disco.
        self.local_yaml_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_yaml_path.write_text(
            _render_deilebot_yaml(cfg), encoding="utf-8"
        )
        self._write_deploy_state("local")
        self._write_env(cfg)
        self._out(
            "\n[ok] Configuração local gravada:\n"
            f"   {self.env_path}\n"
            f"   {self.local_yaml_path}\n"
        )
        deploy = self.deploy_script_path
        if deploy.is_file() and self._confirm(
            "Subir o bot como serviço de segundo plano agora?", default=True
        ):
            self._out("")
            try:
                # to_thread: subprocess.run é bloqueante — não pode
                # travar o event loop dentro de uma coroutine.
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, str(deploy), "start", "--target", "local"],
                    cwd=str(self.root),
                )
            except OSError as exc:
                self._out(f"  [x] não consegui executar o deploy.py: {exc}")
                return 1
            return proc.returncode
        self._out(
            "\nQuando quiser iniciar o bot:\n"
            "   python -m deilebot run --provider discord     (em foreground)\n"
            "   python3 infra/k8s/deploy.py start             (como serviço)\n"
        )
        return 0

    async def _apply_container(self, cfg: WizardConfig) -> int:
        deploy = self.deploy_script_path
        if not deploy.is_file():
            raise SetupError(
                f"não achei {deploy} — rode o wizard a partir da raiz do "
                "repositório `deile` (onde fica a pasta infra/)."
            )
        # Ordem deliberada: o ConfigMap e o deploy-state (sem segredo) são
        # escritos primeiro; o `.env` (token do Discord + chave de LLM)
        # por ÚLTIMO — se _patch_bot_configmap falhar (ex.: bloco
        # `owners:` ausente), nenhum segredo terá ido para disco. O
        # deploy.py só lê o `.env` mais adiante, em _deploy_container.
        self._patch_bot_configmap(cfg)
        self._write_deploy_state("container")
        self._write_env(cfg)
        self._out(
            "\n[ok] Escrito:\n"
            f"   {self.env_path}\n"
            f"   {self.bot_configmap_path}  (donos + github)\n"
            f"   {self.deploy_state_path}\n"
        )
        self._out(
            "\nPróximo: buildar a imagem e aplicar a stack no Kubernetes.\n"
            "O deploy.py cuida dos pré-requisitos — instala o que faltar.\n"
        )
        if not self._confirm("Buildar e fazer o deploy agora?", default=True):
            self._out(
                "\nConfig gravada; deploy não executado. Quando quiser:\n"
                "   python3 infra/k8s/deploy.py build\n"
                "   python3 infra/k8s/deploy.py up\n"
            )
            return 0
        return await self._deploy_container()

    def _patch_bot_configmap(self, cfg: WizardConfig) -> None:
        path = self.bot_configmap_path
        if not path.is_file():
            raise SetupError(f"ConfigMap não encontrado: {path}")
        text = path.read_text(encoding="utf-8")
        # Casa o bloco `owners:` inteiro (a chave + as entradas discord:)
        # e o reescreve — funciona com 1 ou N entradas pré-existentes.
        pattern = re.compile(
            r'(?P<lead>\r?\n)(?P<ind>[ \t]*)owners:[ \t]*'
            r'(?:(?:\r?\n)[ \t]*-[ \t]*"discord:[^"]*")+'
        )

        def _repl(m: "re.Match") -> str:
            ind = m.group("ind")
            entries = "".join(
                f'\n{ind}  - "discord:{o}"' for o in cfg.owner_ids
            )
            return m.group("lead") + ind + "owners:" + entries

        text, n = pattern.subn(_repl, text, count=1)
        if n == 0:
            raise SetupError(
                f"não achei o bloco `owners:` em {path} — ajuste à mão."
            )
        text = re.sub(
            r'oauth_client_id:\s*".*?"',
            f'oauth_client_id: "{cfg.github_oauth_client_id}"',
            text, count=1,
        )
        path.write_text(text, encoding="utf-8")

    async def _deploy_container(self) -> int:
        deploy = str(self.deploy_script_path)
        for stage in ("build", "up"):
            self._out(f"\n>> python3 infra/k8s/deploy.py {stage}\n")
            try:
                # to_thread: subprocess.run (build/up demoram) é bloqueante
                # — não pode travar o event loop dentro de uma coroutine.
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, deploy, stage, "--yes"],
                    cwd=str(self.root),
                )
            except OSError as exc:
                self._out(f"  [x] não consegui executar o deploy.py: {exc}")
                return 1
            if proc.returncode != 0:
                self._out(
                    f"  [x] `deploy.py {stage}` falhou (exit {proc.returncode}).\n"
                    "      Veja a saída acima e o README, seção 'Deploy em\n"
                    "      Kubernetes'."
                )
                return proc.returncode
        self._out(
            "\n[ok] Deploy concluído. Verifique:\n"
            "   python3 infra/k8s/deploy.py status\n"
            "\nO bot já deve estar online no Discord.\n"
        )
        return 0

    # ----- detecção -----

    def _detect_existing(self) -> bool:
        """True quando o `.env` já tem um token do Discord não-vazio."""
        if not self.env_path.is_file():
            return False
        text = self.env_path.read_text(encoding="utf-8", errors="replace")
        return bool(
            re.search(r"^\s*DEILE_BOT_DISCORD_TOKEN\s*=\s*\S", text, re.M)
        )


def run_setup(
    mode: Optional[str] = None,
    reconfigure: bool = False,
    root: Optional[Path] = None,
) -> int:
    """Ponto de entrada síncrono para o subcomando `setup` da CLI."""
    wizard = SetupWizard(root or Path.cwd())
    try:
        return asyncio.run(wizard.run(mode=mode, reconfigure=reconfigure))
    except SetupError as exc:
        print(f"deilebot setup: {exc}", file=sys.stderr)
        return 78
    except KeyboardInterrupt:
        print("\ndeilebot setup: cancelado.", file=sys.stderr)
        return 130
