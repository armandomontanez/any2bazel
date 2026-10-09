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

"""Windows command-line tokenization.

MSBuild logs tool invocations as ONE string, quoted the Windows way, where a
quote can open in the middle of a token and a backslash is only special before
a quote:

    /I"x64\\Release\\Generated Files\\\\"      ->  /Ix64\\Release\\Generated Files\\
    /analyze:rulesetdirectory";C:\\Program Files (x86)\\...\\Rule Sets;"   (one token)

POSIX splitters (`shlex`) get both wrong. This module implements the rules the
Microsoft C runtime and `CommandLineToArgvW` use, which are also what cl.exe and
link.exe apply to their own command lines and response files.

Pure string work with no OS dependency, so it behaves the same on any host.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

_WHITESPACE = " \t\r\n"

# An unquoted tool path ends at the first ".exe" (or .bat/.cmd) followed by
# whitespace or end of string. MSBuild logs e.g.
#   C:\Program Files (x86)\...\HostX64\x64\CL.exe /c /I..\ ...
# with the path unquoted despite the spaces.
_TOOL_END = re.compile(r"\.(exe|bat|cmd|com)(?=\s|$)", re.IGNORECASE)


def split(cmdline: str) -> List[str]:
    """Split a Windows command line into arguments.

    Rules (MSVC runtime, 2008 and later):
      * arguments are separated by whitespace outside quotes (newlines count,
        so a response file's content can be passed as is);
      * `"` toggles quoted mode and is not part of the argument;
      * 2n backslashes followed by `"` produce n backslashes and the quote
        toggles quoted mode; 2n+1 backslashes followed by `"` produce n
        backslashes and a literal quote; backslashes not followed by `"` are
        literal;
      * inside quotes, `""` is a literal quote.
    """
    args: List[str] = []
    cur: List[str] = []
    in_quotes = False
    have_token = False
    i, n = 0, len(cmdline)
    while i < n:
        c = cmdline[i]
        if c == "\\":
            j = i
            while j < n and cmdline[j] == "\\":
                j += 1
            run = j - i
            if j < n and cmdline[j] == '"':
                cur.append("\\" * (run // 2))
                have_token = True
                if run % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j          # the quote is handled on the next pass
            else:
                cur.append("\\" * run)
                have_token = True
                i = j
            continue
        if c == '"':
            if in_quotes and i + 1 < n and cmdline[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
            have_token = True      # "" is an (empty) argument
            i += 1
            continue
        if c in _WHITESPACE and not in_quotes:
            if have_token:
                args.append("".join(cur))
                cur = []
                have_token = False
            i += 1
            continue
        cur.append(c)
        have_token = True
        i += 1
    if have_token:
        args.append("".join(cur))
    return args


def split_tool(cmdline: str) -> Tuple[str, List[str]]:
    """Split a logged tool invocation into (tool path, arguments).

    Handles the unquoted-path-with-spaces form MSBuild logs. Returns ("", args)
    when the line carries no recognizable tool (a bare switch list, as stored
    in tracking logs).
    """
    s = cmdline.lstrip()
    if not s:
        return "", []
    if s[0] == '"':
        args = split(s)
        return (args[0], args[1:]) if args else ("", [])
    if s[0] in "/-" or s[0] == "@":
        return "", split(s)
    m = _TOOL_END.search(s)
    if m:
        return s[:m.end()], split(s[m.end():])
    args = split(s)
    return (args[0], args[1:]) if args else ("", [])


def _read_response_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


def expand_response_files(
        args: List[str], resolve: Callable[[str], str] = lambda p: p,
        read: Callable[[str], Optional[str]] = _read_response_file,
        _depth: int = 0) -> List[str]:
    """Replace `@file` arguments with the arguments the file contains.

    `resolve` maps the path as written to one that can be opened (typically
    joining it to the action's working directory). An unreadable file is left
    as `@file` rather than dropped.
    """
    out: List[str] = []
    for a in args:
        if a.startswith("@") and len(a) > 1 and _depth < 4:
            text = read(resolve(a[1:]))
            if text is not None:
                out.extend(expand_response_files(split(text), resolve, read, _depth + 1))
                continue
        out.append(a)
    return out
