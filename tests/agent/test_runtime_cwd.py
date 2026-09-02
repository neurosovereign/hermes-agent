"""Tests for agent/runtime_cwd.py — the single source of truth for the agent working directory."""

import os
from pathlib import Path

import pytest

import agent.runtime_cwd as rt
from agent.runtime_cwd import (
    clear_session_cwd,
    resolve_agent_cwd,
    resolve_context_cwd,
    set_session_cwd,
)


def _raise_oserror(*args, **kwargs):
    raise OSError("cwd gone")


class TestResolveAgentCwd:
    def test_prefers_terminal_cwd_over_getcwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.chdir(os.path.expanduser("~"))
        assert resolve_agent_cwd() == tmp_path





    def test_propagates_oserror_from_getcwd(self, monkeypatch):
        # The fallback arm calls os.getcwd(), which can raise OSError (deleted cwd).
        # The resolver must NOT swallow it — build_environment_hints owns the
        # try/except OSError guard at the call site (prompt_builder.py:805).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.setattr(rt.os, "getcwd", _raise_oserror)
        with pytest.raises(OSError):
            resolve_agent_cwd()


class TestResolveContextCwd:
    def test_returns_dir_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert resolve_context_cwd() == tmp_path




    def test_expands_leading_tilde(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", "~")
        assert resolve_context_cwd() == Path(os.path.expanduser("~"))



class TestSessionCwdOverride:
    """The #29531 per-session arm: a contextvar cwd wins over TERMINAL_CWD so a
    multi-session gateway can pin each session to its own folder."""

    def test_session_cwd_overrides_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            assert resolve_agent_cwd() == other
            assert resolve_context_cwd() == other
        finally:
            rt._SESSION_CWD.reset(token)


    def test_clear_session_cwd_restores_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            clear_session_cwd()
            assert resolve_agent_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)



class TestResolveProfileTerminalCwd:
    """Multiplex-gateway arm: a session bound to a non-launch profile must take
    its workspace from THAT profile's config.yaml, not the process-global
    TERMINAL_CWD (same bug class as #40334 in the TUI gateway)."""

    @pytest.fixture()
    def profile_tree(self, tmp_path, monkeypatch):
        import hermes_cli.profiles as profiles_mod

        home = tmp_path / "hermes"
        profiles_root = home / "profiles"
        monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
        monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: home)
        return {"home": home, "profiles_root": profiles_root}

    def _mkprofile(self, profiles_root, name, cwd_value):
        phome = profiles_root / name
        phome.mkdir(parents=True)
        (phome / "config.yaml").write_text(
            "terminal:\n  cwd: %s\n" % cwd_value, encoding="utf-8"
        )
        return phome

    def test_returns_configured_cwd(self, profile_tree, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        self._mkprofile(profile_tree["profiles_root"], "alpha", str(ws))
        assert rt.resolve_profile_terminal_cwd("alpha") == str(ws)

    def test_placeholder_returns_none(self, profile_tree):
        for placeholder in (".", "auto", "cwd"):
            self._mkprofile(
                profile_tree["profiles_root"], "p_" + placeholder.strip("."), placeholder
            )
        assert rt.resolve_profile_terminal_cwd("p_auto") is None
        assert rt.resolve_profile_terminal_cwd("p_cwd") is None

    def test_missing_config_returns_none(self, profile_tree):
        (profile_tree["profiles_root"] / "noconf").mkdir(parents=True)
        assert rt.resolve_profile_terminal_cwd("noconf") is None

    def test_nonexistent_dir_returns_none(self, profile_tree, tmp_path):
        self._mkprofile(
            profile_tree["profiles_root"], "gone", str(tmp_path / "does-not-exist")
        )
        assert rt.resolve_profile_terminal_cwd("gone") is None

    def test_unknown_profile_returns_none(self, profile_tree):
        assert rt.resolve_profile_terminal_cwd("no-such-profile") is None

    def test_default_profile_uses_default_home(self, profile_tree, tmp_path):
        ws = tmp_path / "default-ws"
        ws.mkdir()
        profile_tree["home"].mkdir(parents=True, exist_ok=True)
        (profile_tree["home"] / "config.yaml").write_text(
            "terminal:\n  cwd: %s\n" % ws, encoding="utf-8"
        )
        assert rt.resolve_profile_terminal_cwd("default") == str(ws)

    def test_env_var_expansion(self, profile_tree, tmp_path, monkeypatch):
        ws = tmp_path / "env-ws"
        ws.mkdir()
        monkeypatch.setenv("NS_TEST_WS", str(ws))
        self._mkprofile(profile_tree["profiles_root"], "envp", "${NS_TEST_WS}")
        assert rt.resolve_profile_terminal_cwd("envp") == str(ws)

    def test_fails_open_on_config_error(self, profile_tree, monkeypatch):
        import hermes_cli.profiles as profiles_mod

        (profile_tree["profiles_root"] / "alpha").mkdir(parents=True)
        (profile_tree["profiles_root"] / "alpha" / "config.yaml").write_text(
            "terminal:\n  cwd: /tmp\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            profiles_mod, "_get_profiles_root",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert rt.resolve_profile_terminal_cwd("alpha") is None
