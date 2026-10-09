# Copyright 2026 EngFlow Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read Visual C++ tracking logs (`<IntDir>\\<name>.tlog\\*.tlog`).

The C++ build keeps, per project, the last command line it used for every
source and what each tool invocation read and wrote. It uses them for its own
up-to-date check, which is what makes them useful here:

  * A binlog only has a command line for what the build actually RAN. A source
    MSBuild skipped as up to date logs nothing -- but MSBuild decided that by
    comparing the command line it would run against `CL.command.1.tlog`, so the
    entry there is the current command line by MSBuild's own check.
  * `*.read.1.tlog` lists every file a tool opened: the headers behind a
    compile, and for a link the full path each library name resolved to
    (including libraries pulled in by `/DEFAULTLIB` directives, which never
    appear on a command line).

Format, shared by all of them: UTF-16 text; a line starting with `^` is a key
(one or more `|`-separated source paths) and the lines up to the next key
belong to it. PATHS ARE UPPER-CASED by MSBuild; switches and macro names are
not. `Cl.items.tlog` (`source;object` per line) keeps true case.

The directory name is mangled for long project names (`AppInsta.F3F6E699.tlog`),
so callers must take it from the `TLogLocation` property or a task's
`TrackerLogDirectory`, never guess it.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple


def decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    # No BOM: tracking logs written through MSBuild tasks can be plain UTF-8,
    # but FileTracker's own are UTF-16LE without one in rare cases.
    if len(data) >= 2 and data[1:2] == b"\x00":
        return data.decode("utf-16-le", "replace")
    return data.decode("utf-8", "replace")


def parse_keyed(text: str) -> List[Tuple[Tuple[str, ...], List[str]]]:
    """Split a tracking log into (key paths, value lines) entries, in file
    order. Lines before the first key are ignored."""
    entries: List[Tuple[Tuple[str, ...], List[str]]] = []
    current: Optional[List[str]] = None
    for line in text.splitlines():
        if line.startswith("^"):
            current = []
            entries.append((tuple(p for p in line[1:].split("|") if p), current))
        elif current is not None and line.strip():
            current.append(line)
    return entries


def parse_commands(text: str) -> Dict[str, str]:
    """`*.command.1.tlog` -> {UPPER-CASED source path: command line}.

    A multi-source key (a link's object list) is stored under EVERY path in it
    and under the "|"-joined key, so a link command can be found by any input.
    The command keeps its line breaks (they are argument separators).
    """
    out: Dict[str, str] = {}
    for keys, lines in parse_keyed(text):
        cmd = "\n".join(lines)
        for k in keys:
            out[k.upper()] = cmd
        if len(keys) > 1:
            out["|".join(k.upper() for k in keys)] = cmd
    return out


def parse_files(text: str) -> Dict[str, List[str]]:
    """`*.read.1.tlog` / `*.write.1.tlog` -> {UPPER-CASED source path: files}.
    Multi-source keys are stored under each path."""
    out: Dict[str, List[str]] = {}
    for keys, lines in parse_keyed(text):
        for k in keys:
            out.setdefault(k.upper(), []).extend(lines)
    return out


def parse_items(text: str) -> Dict[str, str]:
    """`Cl.items.tlog` -> {UPPER-CASED source path: true-case source path}."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        src = line.split(";", 1)[0].strip()
        if src:
            out[src.upper()] = src
    return out


def parse_lastbuildstate(text: str) -> Dict[str, str]:
    """`<project>.lastbuildstate` -> toolset facts plus the build key.

    First line is `Name=Value:Name=Value:...`; second is
    `Configuration|Platform|SolutionDir|`.
    """
    out: Dict[str, str] = {}
    lines = [l for l in text.splitlines() if l.strip()]
    if lines:
        for part in lines[0].split(":"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
    if len(lines) > 1:
        fields = lines[1].split("|")
        for name, value in zip(("Configuration", "Platform", "SolutionDir"), fields):
            out[name] = value
    return out


class TlogDir:
    """One project's tracking-log directory, read lazily and tolerantly: a
    missing directory or file just yields nothing."""

    def __init__(self, path: str):
        self.path = path
        self._names: Optional[Dict[str, str]] = None
        self._cache: Dict[str, object] = {}

    def exists(self) -> bool:
        return os.path.isdir(self.path)

    def _file(self, name: str) -> Optional[str]:
        """Case-insensitive lookup (the tool prefix is `CL`, `Lib` or `lib`
        depending on the MSBuild version)."""
        if self._names is None:
            try:
                self._names = {n.lower(): n for n in os.listdir(self.path)}
            except OSError:
                self._names = {}
        real = self._names.get(name.lower())
        return os.path.join(self.path, real) if real else None

    def _text(self, name: str) -> str:
        path = self._file(name)
        if not path:
            return ""
        try:
            with open(path, "rb") as f:
                return decode(f.read())
        except OSError:
            return ""

    def commands(self, tool: str) -> Dict[str, str]:
        """Command lines of `tool` ('cl', 'link', 'lib', 'rc', 'midl')."""
        key = "cmd:" + tool.lower()
        if key not in self._cache:
            self._cache[key] = parse_commands(self._text("%s.command.1.tlog" % tool))
        return self._cache[key]  # type: ignore[return-value]

    def reads(self, tool: str) -> Dict[str, List[str]]:
        """Files `tool` opened. A static library's tracker is `Lib-link`."""
        key = "read:" + tool.lower()
        if key not in self._cache:
            self._cache[key] = parse_files(self._text("%s.read.1.tlog" % tool))
        return self._cache[key]  # type: ignore[return-value]

    def items(self) -> Dict[str, str]:
        if "items" not in self._cache:
            self._cache["items"] = parse_items(self._text("Cl.items.tlog"))
        return self._cache["items"]  # type: ignore[return-value]

    def lastbuildstate(self) -> Dict[str, str]:
        if "state" not in self._cache:
            state: Dict[str, str] = {}
            for lower, real in (self._names if self._names is not None
                                else self._list()).items():
                if lower.endswith(".lastbuildstate"):
                    state = parse_lastbuildstate(self._text(real))
                    break
            self._cache["state"] = state
        return self._cache["state"]  # type: ignore[return-value]

    def _list(self) -> Dict[str, str]:
        self._file("")
        return self._names or {}

    def failed_last_build(self) -> bool:
        """MSBuild drops an `unsuccessfulbuild` marker when a project fails."""
        return self._file("unsuccessfulbuild") is not None
