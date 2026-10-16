"""The protected `.env` file is the one source of a secret; no secret enters the environment.

Gap #103 with #100 (2026-10-16; core register F48; CWE-526). Until then `panel_app` loaded
`.env` at import with `load_dotenv`, `order_flow` did so at five sites, `gateway_preflight`
once, and the core's `Config.from_env()` again — each copying the whole file, secrets
included, into this process's environment, which every child inherits: `pgrep`, `osascript`,
`pkill`, `open -a`, `git`, the Gate 2 dialog, the docker CLI. None needs a secret.

The rule (the operator's decision A, option 1): a secret is read from `.env` — mode 0600 —
into the object that uses it, never placed in a process environment; a secret found in the
inherited environment is removed at startup with a warning naming the variable, never its
value. The core's `load_env_file` does the reading (settings exported where unset, secrets as
`pydantic.SecretStr`, a file accessible to group or others refused); this module adds the
Anthropic key to the secret names, names ClaudIA's own file — the repository root's, which is
where the operator keeps it — and holds the key for `AsyncAnthropic(api_key=…)`.

Every entry point that runs on its own calls `load()` once: the app (`panel_app`, at import,
because `CLAUDIA_MODEL` and the paths are read at import), the preflight and the gateway
launcher (through `gateway_url()`), and `scripts/replay_eval.py`. The call is idempotent.

Third parties: `crawl4ai/config.py` calls a bare `load_dotenv()` at import, and the venv sits
inside the repository, so its search finds this file. After `load()` has run,
`dotenv.load_dotenv` is a function of the same signature that exports settings and never a
secret, so no later call by anyone can undo the rule.

In tests `tests/conftest.py` points `ENV_FILE` at a stand-in of the suite's own, holding a
placeholder key and nothing else, so no test reads the operator's file (CLA-SEC-009); an
opted-in `live_api` run keeps the real one.

Imports nothing from the rest of `claudia`, and the core lazily, so `conftest` can import it
before anything else.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from ibkr_core_mcp import Config
    from ibkr_core_mcp.config import EnvFile
    from pydantic import SecretStr

log = logging.getLogger(__name__)

ENV_FILE: Path = Path(__file__).resolve().parent.parent / ".env"
"""ClaudIA's `.env`: the repository root's. Pointed at the suite's stand-in by `tests/conftest.py`."""

ANTHROPIC_SECRET_NAMES: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
"""The two credential variables the Anthropic SDK reads (anthropic 0.120.2, `_client.py`)."""

_loaded: EnvFile | None = None


def secret_names() -> tuple[str, ...]:
    """The core's secret names plus the Anthropic SDK's two."""
    from ibkr_core_mcp.config import SECRET_NAMES

    return SECRET_NAMES + ANTHROPIC_SECRET_NAMES


def _remove_inherited_secrets() -> None:
    """Remove every secret name found in the inherited environment, warning by name.

    Option 1 (operator, 2026-10-16): the file only. A value exported by a shell profile or a
    parent process would otherwise reach every child, and would shadow the file. The value is
    never logged.
    """
    for name in secret_names():
        if name in os.environ:
            del os.environ[name]
            log.warning(
                "%s was inherited from the environment and has been removed — ClaudIA reads a "
                "secret only from %s",
                name,
                ENV_FILE,
            )


def _load_dotenv_without_secrets(
    dotenv_path: str | os.PathLike[str] | None = None,
    stream: IO[str] | None = None,
    verbose: bool = False,
    override: bool = False,
    interpolate: bool = True,
    encoding: str | None = "utf-8",
) -> bool:
    """What `dotenv.load_dotenv` is once ClaudIA has loaded: settings, never a secret.

    Same signature as python-dotenv 1.2.2's, so a third party's call keeps working: with no
    path it reads ClaudIA's file (which is what its own search would find from this venv);
    `override` is not honoured — our settings are already set, and a secret is never set.
    Returns True when a file was read, as the original does.
    """
    from ibkr_core_mcp.config import load_env_file

    path = ENV_FILE if dotenv_path is None else Path(dotenv_path)
    return load_env_file(path, secret_names=secret_names()).path is not None


def load() -> EnvFile:
    """Read ClaudIA's `.env`: inherited secrets removed, settings exported, secrets held.

    Idempotent. The result is kept for `anthropic_api_key()` and `model_unreachable_reason()`.
    A refused file (accessible to group or others) is logged by the core; what it means for
    ClaudIA — no model — is said by `warn_if_model_unreachable()` at startup.
    """
    global _loaded
    import dotenv
    import dotenv.main
    from ibkr_core_mcp.config import load_env_file

    _remove_inherited_secrets()
    _loaded = load_env_file(ENV_FILE, secret_names=secret_names())
    dotenv.load_dotenv = _load_dotenv_without_secrets
    dotenv.main.load_dotenv = _load_dotenv_without_secrets
    return _loaded


def _env() -> EnvFile:
    """What `load()` read, loading on the first call."""
    return _loaded if _loaded is not None else load()


def anthropic_api_key() -> SecretStr | None:
    """The Anthropic key from the file, or None when the file holds none or was refused."""
    return _env().secrets.get("ANTHROPIC_API_KEY")


def core_config() -> Config:
    """The core's `Config`, read from ClaudIA's own `.env`.

    `Config.from_env()` with no path searches upward from the core's `config.py` — under the
    developer override that is the core repository's `.env`, not this one — and the Flex
    token would read as "not configured" with nothing saying why. Every `Config` in ClaudIA
    comes from here (held by `test_config_from_env_is_called_only_by_the_loader`). ClaudIA's
    own secret names go with the call: with the core's names alone the Anthropic key was
    exported as a setting the moment the toolkit's `Config` was built (live, 2026-10-16).
    """
    from ibkr_core_mcp import Config

    return Config.from_env(dotenv_path=str(ENV_FILE), secret_names=secret_names())


def model_unreachable_reason() -> str | None:
    """Why ClaudIA cannot reach the model — the file and the fix, in one sentence — or None.

    The one text for the three places that say it: the startup line (`warn_if_model_unreachable`),
    the session's System log, and the error building an agent raises (`ClaudIAAgent`), each
    with its own prefix.
    """
    env = _env()
    if env.refused is not None:
        return env.refused
    if "ANTHROPIC_API_KEY" not in env.secrets:
        return (
            f"ANTHROPIC_API_KEY is not set in {env.path or ENV_FILE} — a secret is read from "
            "that file only, never from the environment"
        )
    return None


def warn_if_model_unreachable() -> str | None:
    """Log the reason at ERROR, once, at startup — beside `warn_if_stale` and its siblings.

    Same family: a state that lets ClaudIA start cleanly and fail later. ClaudIA still
    serves (the design's rule: say it on the terminal and in the System log); every agent
    built in that state raises `Setup required: <reason>`, which `panel_app`'s init path
    shows in the chat.
    """
    reason = model_unreachable_reason()
    if reason is not None:
        log.error("ClaudIA cannot reach the model: %s", reason)
    return reason
