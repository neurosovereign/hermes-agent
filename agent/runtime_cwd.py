"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory
(design #19214/#19242: `terminal.cwd` is bridged once to `TERMINAL_CWD` at
gateway/cron startup). The local-CLI backend deliberately leaves it unset and
relies on the launch dir. Reading it in one place keeps the system prompt, the
tool surfaces, and context-file discovery agreeing on where the agent lives.

Multi-session gateways can pin a logical cwd via the `_SESSION_CWD`
contextvar; CLI/cron fall through to `TERMINAL_CWD`/launch cwd.
"""

import logging
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)

# The Python package/source root (this file lives at <root>/agent/runtime_cwd.py).
# When a backend is launched from, or self-spawns into, this tree (the desktop
# app default), an os.getcwd() fallback would inject this repo's contributor
# AGENTS.md as authoritative project context. Context discovery must never
# resolve here.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_install_tree(p: Path) -> bool:
    # True only when p IS the package root or sits inside it. Ancestors of the
    # package root (a user home that happens to contain the checkout, a --user
    # site-packages parent) are legitimate workspaces and must not be blocked.
    try:
        p = p.resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def _session_cwd_override() -> str:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return ""
    return str(value).strip()


def _terminal_cwd_env() -> str:
    """Scope-aware TERMINAL_CWD read (tools.terminal_scope.terminal_env).

    Under gateway multiplexing the per-turn terminal scope carries the active
    profile's cwd; the process-global env var may hold another profile's
    value. Only an import failure falls back: an active refusal scope must
    raise, not silently resolve the launch profile's cwd.
    """
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def scope_terminal_cwd() -> str:
    """Public wrapper — the scope-aware TERMINAL_CWD value (may be empty).

    Shared by agent_init / skill_utils / code_execution_tool so every cwd
    consumer reads through the per-turn terminal scope under gateway
    multiplexing instead of the process-global env var.
    """
    return _terminal_cwd_env()


def resolve_agent_cwd() -> Path:
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
        logger.warning("configured working directory does not exist: %s", override)
    raw = _terminal_cwd_env().strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        logger.warning("TERMINAL_CWD does not exist: %s", raw)
    return Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    # None means "no configured cwd": build_context_files_prompt then falls back
    # to the launch dir (os.getcwd()), correct for a local CLI launched inside a
    # real project. A configured path is validated here (previously it was passed
    # through unchecked, diverging from resolve_agent_cwd). An explicitly
    # configured path is otherwise honored verbatim — including the Hermes
    # source tree itself, which is a legitimate workspace when the user is
    # developing Hermes (per-surface policy for fallback-picked directories
    # lives in build_context_files_prompt; see #64590).
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if not p.is_dir():
            logger.warning("configured working directory does not exist: %s", override)
        else:
            return p
        return None
    raw = _terminal_cwd_env().strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_dir():
            logger.warning("TERMINAL_CWD does not exist: %s", raw)
        else:
            return p
    return None


# Placeholder ``terminal.cwd`` values that don't name a real directory — the
# gateway resolves these to the home dir at runtime, so they must NOT be
# treated as an explicit workspace (mirrors the config bridge in
# gateway/run.py and tui_gateway/server.py).
_CWD_PLACEHOLDERS = {".", "auto", "cwd"}


def configured_cwd_from_cfg(cfg) -> str | None:
    """Return an absolute, existing ``terminal.cwd`` from a config mapping.

    Returns None for placeholders (``.``/``auto``/``cwd``), missing values, or
    paths that don't resolve to a real directory.
    """
    if not isinstance(cfg, dict):
        return None
    terminal_cfg = cfg.get("terminal")
    if not isinstance(terminal_cfg, dict):
        return None
    raw = str(terminal_cfg.get("cwd") or "").strip()
    if not raw or raw in _CWD_PLACEHOLDERS:
        return None
    resolved = os.path.abspath(os.path.expanduser(raw))
    return resolved if os.path.isdir(resolved) else None


def resolve_profile_terminal_cwd(profile_name: str) -> str | None:
    """Resolve a *named* profile's ``terminal.cwd`` from its own config.yaml.

    Multiplex gateways serve every profile from one process, so the
    process-global ``TERMINAL_CWD`` belongs to the *launch* profile. A session
    bound to another profile must take its workspace from THAT profile's
    config, not the stale env var (same bug class as #40334 in the TUI
    gateway). Returns an absolute, existing directory, or None for
    placeholders / missing / invalid paths — callers fall back to legacy
    resolution in that case, preserving single-profile behavior.

    Reads the profile's config.yaml directly (not ``load_config()``, which
    resolves the *active* profile) and fails open on any error.
    """
    try:
        from hermes_cli.profiles import _get_default_hermes_home, _get_profiles_root

        if profile_name == "default":
            profile_home = _get_default_hermes_home()
        else:
            profile_home = _get_profiles_root() / profile_name
        cfg_path = Path(profile_home) / "config.yaml"
        if not cfg_path.exists():
            return None

        from hermes_cli.config import _expand_env_vars, read_user_config_raw

        data = read_user_config_raw(cfg_path)
        expanded = _expand_env_vars(data)
        if isinstance(expanded, dict):
            data = expanded
        return configured_cwd_from_cfg(data)
    except Exception:
        logger.debug(
            "could not resolve terminal.cwd for profile %r", profile_name,
            exc_info=True,
        )
        return None
