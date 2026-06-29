#!/usr/bin/env python3
"""PreToolUse guard: keep environment secrets out of Claude's context.

Registered in ``.claude/settings.json`` for the file/command tools. Once this
repository is public, the most likely way a contributor's local secrets leak is
an assistant accidentally pulling a real ``.env`` file or the process
environment into a transcript or a generated commit. This hook makes that hard.

Claude Code PreToolUse hook contract:
  * stdin  -- JSON describing the pending tool call.
  * exit 0 -- allow the call to proceed (normal permission flow continues).
  * exit 2 -- BLOCK the call; whatever is written to stderr is shown to the
              model as the reason it was denied.

What it blocks:
  * Reading / editing / writing a real env file -- ``.env``, ``.env.local``,
    ``app.env``, ``config.env.local``, ``.envrc``, ``/proc/<pid>/environ`` and
    friends. Secret-free templates (``.env.example``, ``.env.sample``,
    ``.env.template``, ``.env.dist``) and ordinary code files that merely happen
    to be named ``env`` (``env.py`` -- e.g. Alembic's ``migrations/env.py`` --
    ``env.mjs``, ``env.ts``) are allowed.
  * Bash commands that reference a real env file, that dump the whole
    environment (``env`` / ``printenv`` / ``export`` / ``declare`` / ``typeset``
    / bare ``set``, including ``/usr/bin/env`` and ``command env`` forms), that
    read ``/proc/<pid>/environ``, or that recursively grep the tree in a way that
    would slurp a hidden ``.env`` (``grep -r``, ``rg --no-ignore``/``--hidden``).
  * Grep whose ``path`` / ``glob`` targets a real env file or every dotfile.
  * Local code-execution tools (``mcp__ide__executeCode``) whose code reads an
    env file or bulk-dumps ``os.environ``.

It deliberately does NOT block every ``$VAR`` expansion or targeted single-var
read (``echo $HOME``, ``printenv PATH``) -- that would break ordinary commands
for no real gain, since a secret only reaches the model if something dumps or
prints it. The goal is to stop accidental bulk leaks by a cooperative assistant,
not to be a security sandbox.

Known residuals (out of scope -- a cooperative assistant won't hit these, and an
adversary isn't the threat model): deliberate obfuscation (``.e''nv``,
base64-encoded commands), ``find ... -exec cat`` style traversal, ``cat .*``
dotglob slurps, and arbitrary code-exec MCP tools other than the one listed.

Failure mode: any unexpected internal error fails OPEN (exit 0). The threat
model is accidental disclosure by a cooperative assistant, not an adversary
trying to crash the guard to bypass it, so a bug here must not brick the session.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import PurePosixPath

# Template / example files carry no secrets and are meant to be read.
_TEMPLATE_SUFFIXES = (
    ".example",
    ".sample",
    ".template",
    ".tmpl",
    ".dist",
    ".defaults",
)


def _clean(token: str) -> str:
    """Strip surrounding quotes/whitespace and leading shell sigils from a token."""
    return token.strip().strip("\"'").lstrip("@<>")


def is_env_file(path: str) -> bool:
    """True if *path* points at a real, secret-bearing env file (not a template).

    Classifies by dot-delimited segments so it catches the whole family without
    being fooled by glob tokens or extra segments:
        .env  .env.local  .env.production  app.env  config.env.local  .env*  *.env
    while leaving alone:
      * non-env names (environment.py, venv/, a bare ``env`` binary);
      * ordinary code files merely *named* env -- env.py (Alembic!), env.mjs,
        env.ts -- where ``env`` is the leading stem of a normal file;
      * secret-free templates (.env.example, config.env.dist, ...).
    """
    name = PurePosixPath(_clean(path)).name.lower()
    if not name:
        return False
    # Allowlist templates first: e.g. .env.example, .env.sample, config.env.dist.
    if name.endswith(_TEMPLATE_SUFFIXES):
        return False
    # direnv
    if name == ".envrc":
        return True
    # A bare token with no dot (env binary, venv dir, "environment") is not a file.
    if "." not in name:
        return False
    segments = name.split(".")
    for i, segment in enumerate(segments):
        if segment.strip("*?[]") != "env":
            continue
        # ``env`` as the *leading stem* of an ordinary file (env.py, env.mjs,
        # env.ts) is a code file, NOT a secret env file. A real leading-dot env
        # file (.env, .env.local) has an empty first segment, so its ``env``
        # segment sits at index >= 1; ``app.env`` / ``config.env.local`` have
        # ``env`` at index >= 1 too. Only skip when ``env`` is the very first
        # segment of a non-dotfile name.
        if i == 0 and not name.startswith("."):
            continue
        return True
    return False


# Reading the live process environment via /proc (incl. per-thread task paths).
_PROC_ENVIRON_RE = re.compile(r"/proc/[^\s]*?/environ\b")

# Command-position anchor and end-of-command-word, reused by the dump patterns.
_CMD_START = r"(?:^|[\n;&|(]|&&|\|\|)\s*"
_END = r"\s*(?:$|[|;&\n>])"

# Commands that dump variables WITH VALUES (the whole environment, or all
# exported/shell vars). Each is anchored so ordinary uses are left alone:
#   - `/usr/bin/env python`, `env FOO=bar cmd`   -> run a command, not a dump
#   - `printenv PATH`                            -> targeted lookup, allowed
#   - `export FOO=bar`, `declare -x FOO=bar`     -> set a var, not a dump
#   - `set -euo pipefail`                        -> shell options, not a dump
_ENV_DUMP_RES = (
    # env / /usr/bin/env / command env, bare or with only option flags (env -0).
    re.compile(_CMD_START + r"(?:command\s+)?(?:/\S*/)?env(?:\s+-\S+)*" + _END),
    # printenv with no argument = full dump.
    re.compile(_CMD_START + r"(?:/\S*/)?printenv" + _END),
    # export with no args or -p = print exported vars with values.
    re.compile(_CMD_START + r"export(?:\s+-p)?" + _END),
    # declare / typeset bare or with only -p/-x style flags = dump with values.
    re.compile(_CMD_START + r"(?:declare|typeset)(?:\s+-[a-zA-Z]+)*" + _END),
    # bare `set` = dump all shell + env vars with values.
    re.compile(_CMD_START + r"set" + _END),
)

# Recursive tree readers that descend into hidden, gitignored .env files.
# GNU `grep -r` reads hidden files and ignores .gitignore; `rg` only does so when
# told to via --hidden / --no-ignore / -u(u). `git grep` is excluded by the
# caller (it only searches tracked files, and .env is gitignored).
_RECURSIVE_READ_RES = (
    re.compile(r"\b(?:grep|egrep|fgrep|rgrep)\b[^|;&\n]*\s-[a-zA-Z]*[rR]"),
    re.compile(r"\brg\b[^|;&\n]*\s(?:--hidden|--no-ignore|--unrestricted|-[a-zA-Z]*u)\b"),
)

# Bulk dump of os.environ in executed code: print(os.environ), dict(os.environ),
# os.environ.copy(), `for k in os.environ`. A narrowed access (os.environ.get(),
# os.environ['X']) is a targeted single-var read and is left alone.
_PY_ENVIRON_DUMP_RE = re.compile(r"os\.environ\b(?!\s*\.\s*get\b|\s*\[)")
_PY_DOTENV_RE = re.compile(r"\b(?:load_dotenv|dotenv_values)\s*\(")

# Break a command/code line into path-ish tokens to test against is_env_file().
_TOKEN_SPLIT_RE = re.compile(r"[\s=:,;&|()<>]+")


def _proc_reason(text: str) -> str | None:
    if _PROC_ENVIRON_RE.search(text):
        return "it reads the live process environment via /proc/<pid>/environ"
    return None


def _env_file_token_reason(text: str) -> str | None:
    for raw in _TOKEN_SPLIT_RE.split(text):
        tok = _clean(raw)
        if tok and is_env_file(tok):
            return f"it references the env file {tok!r}"
    return None


def bash_reason(command: str) -> str | None:
    """Return a denial reason if *command* would read env secrets, else None."""
    reason = _proc_reason(command)
    if reason:
        return reason
    for rx in _ENV_DUMP_RES:
        if rx.search(command):
            return (
                "it dumps environment variables with their values "
                "(env / printenv / export / declare / typeset / set)"
            )
    if "git grep" not in command:
        for rx in _RECURSIVE_READ_RES:
            if rx.search(command):
                return (
                    "it recursively reads the tree in a way that would include a "
                    "hidden .env (use the Grep tool, which skips .env, or add "
                    "--exclude='.env*')"
                )
    return _env_file_token_reason(command)


def code_reason(code: str) -> str | None:
    """Return a denial reason if executed *code* would read env secrets, else None."""
    reason = _proc_reason(code)
    if reason:
        return reason
    if _PY_ENVIRON_DUMP_RE.search(code):
        return "it bulk-dumps the process environment (os.environ)"
    if _PY_DOTENV_RE.search(code):
        return "it loads a .env file (load_dotenv / dotenv_values)"
    return _env_file_token_reason(code)


def deny(reason: str) -> None:
    """Block the tool call: explain to the model and exit with the block code."""
    sys.stderr.write(
        "Blocked by .claude/hooks/guard-env.py: this action would expose "
        f"environment secrets because {reason}.\n"
        "Real .env files and the process environment are intentionally "
        "off-limits to prevent leaking local credentials into the transcript "
        "or a commit. Use the committed .env.example for variable names, or ask "
        "the user to share any specific value you need.\n"
    )
    raise SystemExit(2)


# Tools whose primary argument is a file path we should classify directly.
_FILE_TOOLS = ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit")


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return  # nothing to inspect -> allow
    event = json.loads(raw)
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}

    if tool in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if _PROC_ENVIRON_RE.search(path) or is_env_file(path):
            deny(f"it targets the env file {path!r}")

    elif tool == "Bash":
        reason = bash_reason(str(tool_input.get("command", "")))
        if reason:
            deny(reason)

    elif tool == "Grep":
        for key in ("path", "glob"):
            value = str(tool_input.get(key) or "")
            if value and is_env_file(value):
                deny(f"its {key} targets the env file {value!r}")
        # A bare-dotfile glob (.* and friends) would pull every hidden file in,
        # including .env. A scoped dotted path like .github/**/*.yml is fine.
        if PurePosixPath(str(tool_input.get("glob") or "")).name == ".*":
            deny("its glob matches every dotfile, which would include .env")

    elif tool == "mcp__ide__executeCode":
        reason = code_reason(str(tool_input.get("code", "")))
        if reason:
            deny(reason)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # fail OPEN: a guard bug must not brick the session
        sys.stderr.write(f"guard-env.py: internal error, allowing call: {exc}\n")
        # exit 0 (default) -> allow
