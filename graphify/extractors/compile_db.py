"""compile_commands.json discovery and parsing for C/C++ include resolution."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from graphify.detect import _find_vcs_root


@dataclass
class CompileEntry:
    directory: Path
    include_dirs: list[Path] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)


_COMPILE_COMMANDS_SEARCH_PATHS = [
    "build/compile_commands.json",
    "build/Release/compile_commands.json",
    "build/Debug/compile_commands.json",
    "build/RelWithDebInfo/compile_commands.json",
    "build/MinSizeRel/compile_commands.json",
    "out/build/compile_commands.json",
    "compile_commands.json",
]

_I_FLAG_RE = re.compile(r"(?:^|\s)-I\s*(\S+)")


def _parse_include_flags(raw_command: str) -> list[str]:
    """Extract -I <path> and -I<path> flags from a compile command string."""
    paths: list[str] = []
    for m in _I_FLAG_RE.finditer(raw_command):
        p = m.group(1).strip('"\'')
        if p:
            paths.append(p)
    return paths


def _parse_defines(raw_command: str) -> dict[str, str]:
    """Extract -D NAME=VALUE flags from a compile command string."""
    defines: dict[str, str] = {}
    for m in re.finditer(r"(?:^|\s)-D\s*(\S+)", raw_command):
        raw_def = m.group(1)
        if "=" in raw_def:
            name, _, value = raw_def.partition("=")
            defines[name] = value.strip('"\'')
        else:
            defines[raw_def] = ""
    return defines


def discover_compile_commands(
    root: Path,
    *,
    explicit_path: Path | None = None,
) -> dict[Path, CompileEntry] | None:
    """Discover and parse compile_commands.json for include resolution.

    Priority order (RFC tiers):
      1. ``explicit_path`` — CLI --compile-commands or GRAPHIFY_COMPILE_COMMANDS
      2. Auto-discovery — walk upward from *root* to VCS root, checking
         conventional locations
      3. Fallback — returns None (current same-directory-only behavior)

    Returns a dict mapping source file ``Path`` -> ``CompileEntry``, or None
    when no database is available.
    """
    db_path: Path | None = None

    if explicit_path is not None:
        if explicit_path.is_dir():
            candidate = explicit_path / "compile_commands.json"
            if candidate.is_file():
                db_path = candidate
            else:
                print(
                    f"[graphify extract] error: --compile-commands path "
                    f"{explicit_path} is a directory without compile_commands.json",
                    file=sys.stderr,
                )
                return None
        elif explicit_path.is_file():
            db_path = explicit_path
        else:
            print(
                f"[graphify extract] error: --compile-commands path "
                f"{explicit_path} does not exist",
                file=sys.stderr,
            )
            return None
    else:
        vcs_root = _find_vcs_root(root)
        ceiling = vcs_root.parent if vcs_root else Path(root.resolve().anchor)
        current = root.resolve()
        while True:
            for rel_path in _COMPILE_COMMANDS_SEARCH_PATHS:
                candidate = current / rel_path
                if candidate.is_file():
                    db_path = candidate
                    break
            if db_path is not None:
                break
            if current == ceiling or current.parent == current:
                break
            current = current.parent

    if db_path is None:
        return None

    try:
        with open(db_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"[graphify extract] warning: failed to parse {db_path}: {exc}",
            file=sys.stderr,
        )
        return None

    if not data:
        print(
            f"[graphify extract] warning: {db_path} contains zero entries",
            file=sys.stderr,
        )
        return None

    entries: dict[Path, CompileEntry] = {}
    seen = set()
    include_dir_set: set[str] = set()

    for item in data:
        src_file = item.get("file")
        if not src_file:
            continue
        directory = item.get("directory", "")
        command = item.get("command", "")
        if not command:
            arguments = item.get("arguments", [])
            if arguments:
                command = " ".join(arguments)

        dir_path = Path(directory)

        entry_key = Path(src_file)
        if not entry_key.is_absolute():
            entry_key = (dir_path / entry_key).resolve()
        else:
            entry_key = entry_key.resolve()
        include_paths_raw = _parse_include_flags(command)

        include_dirs: list[Path] = []
        for inc in include_paths_raw:
            inc_path = Path(inc)
            if not inc_path.is_absolute():
                inc_path = (dir_path / inc_path).resolve()
            else:
                inc_path = inc_path.resolve()
            include_dirs.append(inc_path)
            include_dir_set.add(str(inc_path))

        if entry_key not in seen:
            seen.add(entry_key)
            entries[entry_key] = CompileEntry(
                directory=dir_path.resolve(),
                include_dirs=[p for p in include_dirs if p.is_dir()],
                defines=_parse_defines(command),
            )

    if entries:
        print(
            f"[graphify extract] using compile_commands.json at {db_path} "
            f"({len(entries)} entries, {len(include_dir_set)} unique include dirs)"
        )

    return entries if entries else None
