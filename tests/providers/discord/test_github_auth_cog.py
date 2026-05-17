"""Smoke tests do GitHubAuthCog — construção e wiring (sem Discord ao vivo)."""

from __future__ import annotations


class TestGitHubAuthCogShape:
    def test_declares_expected_commands(self):
        """O cog declara os três comandos hybrid esperados."""
        from deilebot.providers.discord.cogs.github_auth_cog import \
            GitHubAuthCog

        names = set()
        for attr_name in dir(GitHubAuthCog):
            attr = getattr(GitHubAuthCog, attr_name)
            if hasattr(attr, "name") and not attr_name.startswith("_"):
                names.add(getattr(attr, "name", None))
        assert "github_login" in names
        assert "github_status" in names
        assert "github_logout" in names

    def test_construct_without_discord(self):
        from deilebot.providers.discord.cogs.github_auth_cog import \
            GitHubAuthCog

        cog = GitHubAuthCog.__new__(GitHubAuthCog)
        cog.bot = None
        cog.runtime = None
        cog.adapter = None
        assert hasattr(cog, "github_login")
        assert hasattr(cog, "github_status")
        assert hasattr(cog, "github_logout")


class TestGitHubAuthCogWiring:
    """Garante que adapter.start() registra o GitHubAuthCog.

    O closure on_ready não é invocável sem um cliente Discord vivo, então
    inspecionamos o fonte de DiscordAdapter.start — uma regressão (alguém
    removendo o add_cog) quebra o teste.
    """

    def test_cog_is_wired_in_adapter(self):
        import inspect

        from deilebot.providers.discord.adapter import DiscordAdapter

        src = inspect.getsource(DiscordAdapter.start)
        assert "GitHubAuthCog" in src
        assert "add_cog(GitHubAuthCog(" in src
