import asyncio
import os

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    get_session_env,
    set_session_vars,
    clear_session_vars,
    reset_session_vars,
    _VAR_MAP,
    _UNSET,
)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Reset all session contextvars to _UNSET between tests.

    In production each asyncio.Task gets a fresh context copy where the
    defaults are _UNSET.  In tests all functions share the same thread
    context, so a clear_session_vars() from test A (which sets vars to "")
    would leak into test B.  This fixture ensures each test starts clean.
    """
    yield
    for var in _VAR_MAP.values():
        # Can't use var.reset() without a token; just set back to sentinel.
        var.set(_UNSET)


def test_set_session_env_sets_contextvars(monkeypatch):
    """_set_session_env should populate contextvars, not os.environ."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_TYPE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    tokens = runner._set_session_env(context)

    # Values should be readable via get_session_env (contextvar path)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_SOURCE") == ""
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == "Group"
    assert get_session_env("HERMES_SESSION_CHAT_TYPE") == "group"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"
    assert get_session_env("HERMES_SESSION_USER_NAME") == "alice"
    assert get_session_env("HERMES_SESSION_THREAD_ID") == "17585"

    # os.environ should NOT be touched
    assert os.getenv("HERMES_SESSION_PLATFORM") is None
    assert os.getenv("HERMES_SESSION_SOURCE") is None
    assert os.getenv("HERMES_SESSION_CHAT_TYPE") is None
    assert os.getenv("HERMES_SESSION_THREAD_ID") is None

    # Clean up
    runner._clear_session_env(tokens)


def test_clear_session_env_restores_previous_state(monkeypatch):
    """_clear_session_env should restore contextvars to their pre-handler values."""
    runner = object.__new__(GatewayRunner)

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_TYPE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"
    assert get_session_env("HERMES_SESSION_CHAT_TYPE") == "group"

    runner._clear_session_env(tokens)

    # After clear, contextvars should return to defaults (empty)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_CHAT_ID") == ""
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_CHAT_TYPE") == ""
    assert get_session_env("HERMES_SESSION_USER_ID") == ""
    assert get_session_env("HERMES_SESSION_USER_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""


def test_get_session_env_falls_back_to_os_environ(monkeypatch):
    """get_session_env should fall back to os.environ when contextvar is unset."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_PLATFORM") == "discord"

    # Now set a contextvar — should prefer it
    tokens = set_session_vars(platform="telegram")
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"

    # After clear — should return "" (explicitly cleared), NOT fall back
    # to os.environ.  This is the fix for #10304: stale os.environ values
    # must not leak through after a gateway session is cleaned up.
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""


# ---------------------------------------------------------------------------
# SESSION_KEY contextvars tests
# ---------------------------------------------------------------------------


def test_session_key_falls_back_to_os_environ(monkeypatch):
    """get_session_env for SESSION_KEY should fall back to os.environ."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "env-session-123")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_KEY") == "env-session-123"

    # Set contextvar — should prefer it
    tokens = set_session_vars(session_key="ctx-session-456")
    assert get_session_env("HERMES_SESSION_KEY") == "ctx-session-456"

    # After clear — should return "" (explicitly cleared), not os.environ (#10304)
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_session_key_no_race_condition_with_contextvars(monkeypatch):
    """Prove contextvars isolates SESSION_KEY across concurrent async tasks.

    Two tasks set different session keys. With contextvars each task
    reads back its own value. With os.environ the second task would
    overwrite the first (the old bug).
    """
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    results = {}

    async def handler(key: str, delay: float):
        tokens = set_session_vars(session_key=key)
        try:
            await asyncio.sleep(delay)
            read_back = get_session_env("HERMES_SESSION_KEY")
            results[key] = read_back
        finally:
            clear_session_vars(tokens)

    async def run():
        task_a = asyncio.create_task(handler("session-A", 0.15))
        await asyncio.sleep(0.05)
        task_b = asyncio.create_task(handler("session-B", 0.05))
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())

    # Both tasks must read back their own session key
    assert results["session-A"] == "session-A", (
        f"Session A got '{results['session-A']}' instead of 'session-A' — race condition!"
    )
    assert results["session-B"] == "session-B", (
        f"Session B got '{results['session-B']}' instead of 'session-B' — race condition!"
    )


@pytest.mark.asyncio
async def test_run_in_executor_with_context_preserves_session_env(monkeypatch):
    """Gateway executor work should inherit session contextvars for tool routing."""
    runner = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="2144471399",
        chat_type="dm",
        user_id="123456",
        user_name="alice",
        thread_id=None,
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:telegram:dm:2144471399",
    )

    tokens = runner._set_session_env(context)
    try:
        result = await runner._run_in_executor_with_context(
            lambda: {
                "platform": get_session_env("HERMES_SESSION_PLATFORM"),
                "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
                "user_id": get_session_env("HERMES_SESSION_USER_ID"),
                "session_key": get_session_env("HERMES_SESSION_KEY"),
            }
        )
    finally:
        runner._clear_session_env(tokens)
        runner._shutdown_executor()

    assert result == {
        "platform": "telegram",
        "chat_id": "2144471399",
        "user_id": "123456",
        "session_key": "agent:main:telegram:dm:2144471399",
    }




def test_cron_session_contextvar_preserves_legacy_env_fallback(monkeypatch):
    """Unset cron ContextVar keeps old env-only cron callers working."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    assert get_session_env("HERMES_CRON_SESSION") == "1"


def test_cron_session_explicit_blank_masks_leaked_env(monkeypatch):
    """Non-cron session bindings must override a stale process cron env flag."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    tokens = set_session_vars(platform="api_server", cron_session="")
    try:
        assert get_session_env("HERMES_CRON_SESSION") == ""
    finally:
        clear_session_vars(tokens)

    assert get_session_env("HERMES_CRON_SESSION") == ""


def test_cron_session_set_clear_and_reset_tristate(monkeypatch):
    """Cron marker supports _UNSET fallback, 1 cron, and  explicit clear."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    tokens = set_session_vars(cron_session="1")
    assert get_session_env("HERMES_CRON_SESSION") == "1"

    clear_session_vars(tokens)
    assert get_session_env("HERMES_CRON_SESSION") == ""

    reset_session_vars()
    assert get_session_env("HERMES_CRON_SESSION") == "1"



class TestSetSessionEnvProfileCwd:
    """Multiplex gateways: _set_session_env pins the session cwd from the
    SESSION profile's own terminal.cwd (not the launch profile's process-global
    TERMINAL_CWD). When nothing is configured, _SESSION_CWD must resolve via
    the legacy fallback (TERMINAL_CWD env) unchanged."""

    @pytest.fixture()
    def profile_tree(self, tmp_path, monkeypatch):
        import hermes_cli.profiles as profiles_mod

        home = tmp_path / "hermes"
        profiles_root = home / "profiles"
        monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
        monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: home)
        return {"home": home, "profiles_root": profiles_root}

    def _context(self, profile):
        source = SessionSource(
            platform=Platform.MATRIX,
            chat_id="!room:example",
            chat_type="dm",
            user_id="@u:example",
            profile=profile,
        )
        return SessionContext(source=source, connected_platforms=[], home_channels={})

    def test_profile_cwd_pinned(self, profile_tree, tmp_path):
        import agent.runtime_cwd as rt

        ws = tmp_path / "alpha-ws"
        ws.mkdir()
        phome = profile_tree["profiles_root"] / "alpha"
        phome.mkdir(parents=True)
        (phome / "config.yaml").write_text(
            "terminal:\n  cwd: %s\n" % ws, encoding="utf-8"
        )

        runner = object.__new__(GatewayRunner)
        tokens = runner._set_session_env(self._context("alpha"))
        try:
            assert rt.resolve_agent_cwd() == ws
            assert rt.resolve_context_cwd() == ws
        finally:
            runner._clear_session_env(tokens)
            rt._SESSION_CWD.set(_UNSET)

    def _assert_legacy_fallback(self, monkeypatch, tmp_path, context):
        """With no per-profile cwd pinned, resolution must fall back to the
        process-global TERMINAL_CWD exactly as before the fix."""
        import agent.runtime_cwd as rt

        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        runner = object.__new__(GatewayRunner)
        tokens = runner._set_session_env(context)
        try:
            assert rt.resolve_context_cwd() == tmp_path
        finally:
            runner._clear_session_env(tokens)

    def test_profile_without_cwd_keeps_legacy_fallback(self, profile_tree, monkeypatch, tmp_path):
        phome = profile_tree["profiles_root"] / "beta"
        phome.mkdir(parents=True)
        (phome / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
        self._assert_legacy_fallback(monkeypatch, tmp_path, self._context("beta"))

    def test_no_profile_keeps_legacy_fallback(self, profile_tree, monkeypatch, tmp_path):
        self._assert_legacy_fallback(monkeypatch, tmp_path, self._context(None))

    def test_resolver_error_fails_open(self, profile_tree, monkeypatch, tmp_path):
        import agent.runtime_cwd as rt

        monkeypatch.setattr(
            rt,
            "resolve_profile_terminal_cwd",
            lambda name: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        self._assert_legacy_fallback(monkeypatch, tmp_path, self._context("alpha"))
