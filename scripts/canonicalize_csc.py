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

"""Command-line canonicalization for the C# compiler (csc / Roslyn).

One csc invocation builds one assembly, so unlike C/C++ there is no per-source
flag set: the unit of comparison is the whole command line. This module turns
it into a `CscView` -- sources, defines, options, references, analyzers,
resources -- in a form that is the same whether MSBuild's Csc task or
rules_dotnet's `CSharpCompile` action wrote it.

The policy is the one canonicalize_msvc.py follows:

  * `/x` and `-x` are the same option; names are case-insensitive; short forms
    (`/r:`, `/d:`, `/t:`, `/a:`, `/res:`, `/w:`) are their long forms.
  * Single-state options resolve last-wins, and a state equal to the
    compiler's default is written as nothing (`/unsafe-`, `/checked-`,
    `/nullable:disable`, `/warn:4`, `/filealign:512`, `/platform:anycpu`).
    rules_dotnet states several defaults on every line; MSBuild states others.
  * Where the output goes and how the compiler reports are mechanics.
    `/optimize` and `/debug` are build type. Both are dropped.
  * Everything else is kept, including options this file has never heard of.

Lists (`/define:A;B`, `/nowarn:1591,CS0168`) are split, and warning ids are
normalized (`1591` is `CS1591`), so each element compares on its own.
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from canonicalize_msvc import norm_path

_SOURCE_EXTS = (".cs",)

# short or alternate name -> canonical name
_ALIASES = {
    "r": "reference", "d": "define", "t": "target", "a": "analyzer", "res": "resource",
    "linkres": "linkresource", "w": "warn", "o": "optimize", "m": "main", "l": "link",
    "recurse": "recurse", "addmodule": "addmodule", "langversion": "langversion",
}

# Where output goes, how diagnostics are reported, reproducibility plumbing and
# debug-info side channels. None of these change the assembly's code or surface.
_MECHANICS = {
    "out", "refout", "pdb", "noconfig", "nologo", "utf8output", "fullpaths",
    "errorreport", "errorendlocation", "errorlog", "preferreduilang", "sourcelink",
    "embed", "pathmap", "sdkpath", "generatedfilesout", "reportanalyzer", "skipanalyzers",
    "shared", "keepalive", "parallel", "runtimemetadataversion", "checksumalgorithm",
    "reportivts", "bugreport", "codepage",
}
# The C# analog of -O* / -g*.
_BUILD_TYPE = {"optimize", "debug"}

# Options that are on or off (`/x`, `/x+`, `/x-`), with the compiler's default.
_BOOLEAN_DEFAULT = {
    "unsafe": False, "checked": False, "nostdlib": False, "deterministic": False,
    "highentropyva": False, "delaysign": False, "publicsign": False,
    "nowin32manifest": False, "refonly": False,
}
# Options with one value, last one wins; the value that is the default is elided.
_VALUE_DEFAULT = {
    "target": None,             # always shown: exe / winexe / library decide the artifact
    "langversion": None,        # always shown: each toolchain has a different default
    "platform": "anycpu",
    "warn": "4",
    "filealign": "512",
    "nullable": "disable",
    "subsystemversion": None,
    "baseaddress": None,
    "main": None,
    "moduleassemblyname": None,
    "modulename": None,
}
# Value is a file: normalized like any other path.
_PATH_VALUE = {"keyfile", "win32manifest", "win32icon", "win32res", "appconfig", "ruleset"}
# Options whose value names a file (for consumers that compare or relocate it).
PATH_VALUED = frozenset(_PATH_VALUE)

_KNOWN = (set(_ALIASES) | set(_ALIASES.values()) | _MECHANICS | _BUILD_TYPE
          | set(_BOOLEAN_DEFAULT) | set(_VALUE_DEFAULT) | _PATH_VALUE
          | {"define", "reference", "analyzer", "additionalfile", "analyzerconfig",
             "resource", "linkresource", "nowarn", "warnaserror", "doc", "features",
             "keycontainer", "lib", "link", "addmodule", "recurse"})

_WARNING_ID = re.compile(r"^\d+$")
_LIST_SPLIT = re.compile(r"[;,]")
_LANG_INT = re.compile(r"^\d+$")

# A NuGet package (or SDK pack) directory on either side:
#   ...\.nuget\packages\<id>\<version>\lib\...        (PackageReference cache)
#   ...\packages\<Id>.<Version>\lib\...               (packages.config)
#   ...\dotnet\packs\<Id>\<version>\ref\...           (SDK targeting packs)
#   external/<repo>nuget.<id>.v<version>/ref/...      (rules_dotnet packs)
#   external/<repo>/<id>/<version>/lib/...            (a lock-driven repository)
_ASSET_DIRS = r"(?:lib|ref|analyzers|runtimes|build|buildtransitive|tools|contentfiles|data)"
_PKG_PATTERNS = (
    re.compile(r"nuget\.(?P<id>[a-z0-9_.\-]+?)\.v(?P<ver>\d[0-9a-z.\-+]*)/", re.I),
    re.compile(r"/(?P<id>[A-Za-z0-9_.\-]+)/(?P<ver>\d+\.\d+[0-9A-Za-z.\-+]*)/" + _ASSET_DIRS + "/", re.I),
    re.compile(r"/(?P<id>[A-Za-z][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)*)\.(?P<ver>\d+\.\d+[0-9A-Za-z.\-+]*)/"
               + _ASSET_DIRS + "/", re.I),
)

# Packs that ARE the target framework. Their version is a toolchain fact (one
# line in the diff), not a per-reference difference on 160 assemblies.
FRAMEWORK_PACKS = ("microsoft.netcore.app.ref", "microsoft.windowsdesktop.app.ref",
                   "microsoft.aspnetcore.app.ref", "netstandard.library",
                   "netstandard.library.ref", "microsoft.netframework.referenceassemblies")


@dataclass
class CscView:
    """One csc command line, canonical."""
    out: str = ""                       # /out: value, normalized ('' if absent)
    target: str = "exe"
    ref_only: bool = False              # a `/refonly` twin of the real compile
    compiler: str = ""                  # path of csc as written
    sources: Tuple[str, ...] = ()
    defines: Tuple[str, ...] = ()
    options: Tuple[str, ...] = ()       # canonical, sorted
    references: Tuple[str, ...] = ()    # normalized paths
    analyzers: Tuple[str, ...] = ()
    additional_files: Tuple[str, ...] = ()
    analyzer_configs: Tuple[str, ...] = ()
    resources: Tuple[Tuple[str, str], ...] = ()     # (logical name, path)
    raw_references: Tuple[str, ...] = field(default=(), repr=False)  # as written, for package facts
    raw_analyzers: Tuple[str, ...] = field(default=(), repr=False)

    @property
    def assembly(self) -> str:
        """Assembly file name without extension, from `/out:` ('' if unknown)."""
        base = ntpath.basename(self.out.replace("/", "\\"))
        return base[:-4] if base.lower().endswith((".dll", ".exe")) else base


def _basename(tok: str) -> str:
    return ntpath.basename(tok.replace("/", "\\")).lower()


def compiler_index(args: Sequence[str]) -> int:
    """Index of the csc token in an argv, or -1. MSBuild's is `csc.exe` (or
    `dotnet.exe exec csc.dll`); rules_dotnet's is a wrapper script, then
    `dotnet`, then `csc.dll`."""
    for i, tok in enumerate(args[:5]):
        if _basename(tok.strip('"')) in ("csc.exe", "csc.dll", "csc"):
            return i
    return -1


def is_csc(args: Sequence[str]) -> bool:
    return compiler_index(args) >= 0


def _option(tok: str) -> Optional[Tuple[str, Optional[str]]]:
    """`/name[:value]` or `-name[:value]` -> (canonical lower-case name, value).
    None if the token is not an option (a source file, or on a POSIX host a
    path that merely starts with a slash)."""
    if len(tok) < 2 or tok[0] not in "/-":
        return None
    body = tok[1:]
    name, sep, value = body.partition(":")
    sign = ""
    if name[-1:] in "+-" and not sep:
        name, sign = name[:-1], name[-1]
    elif name[-1:] in "+-" and sep:            # /warnaserror+:CS1234
        name, sign = name[:-1], name[-1]
    low = _ALIASES.get(name.lower(), name.lower())
    if low not in _KNOWN:
        # `/work/proj/a.cs` on a POSIX host is a file, not an option.
        if tok[0] == "/" and ("/" in body or tok.lower().endswith(_SOURCE_EXTS)):
            return None
    return low + sign, (value if sep else None)


def _split_list(value: str) -> List[str]:
    return [v.strip().strip('"') for v in _LIST_SPLIT.split(value) if v.strip().strip('"')]


def warning_id(raw: str) -> str:
    """`1591`, `cs1591` and `CS1591` are the same compiler diagnostic. Any other
    id is an analyzer's and is matched as written: `/warnaserror-:CSWINRT1028`
    does not apply to the diagnostic `CsWinRT1028`."""
    raw = raw.strip()
    if _WARNING_ID.match(raw):
        return "CS%04d" % int(raw)
    if raw[:2].upper() == "CS" and raw[2:].isdigit():
        return "CS%04d" % int(raw[2:])
    return raw


def _langversion(value: str) -> str:
    v = value.strip().lower()
    return v + ".0" if _LANG_INT.match(v) else v


def parse(args: Sequence[str], repo_root: str = "", *, cwd: str = "",
          path_map: Iterable[Tuple[str, str]] = ()) -> CscView:
    """Canonical view of one csc command line. `args` may start with the tool
    (and its launcher); everything up to and including csc is skipped."""
    path_map = list(path_map)
    start = compiler_index(args) + 1
    view = CscView(compiler=args[start - 1] if start else "")

    def path(p: str) -> str:
        return norm_path(p.strip('"'), repo_root, cwd, path_map)

    sources: List[str] = []
    defines: Dict[str, None] = {}
    booleans: Dict[str, bool] = {}
    values: Dict[str, str] = {}
    options: List[str] = []
    nowarn: Dict[str, None] = {}
    warn_as_error: Dict[str, None] = {}
    warn_not_as_error: Dict[str, None] = {}
    all_warnings_as_errors: Optional[bool] = None
    references: List[str] = []
    raw_references: List[str] = []
    analyzers: List[str] = []
    raw_analyzers: List[str] = []
    additional: List[str] = []
    configs: List[str] = []
    resources: List[Tuple[str, str]] = []

    for tok in args[start:]:
        if tok.startswith("@"):
            continue                        # an unexpanded response file
        parsed = _option(tok)
        if parsed is None:
            if tok.strip():
                sources.append(path(tok))
            continue
        name, value = parsed
        sign = name[-1] if name[-1] in "+-" else ""
        name = name.rstrip("+-")

        if name == "out":
            view.out = path(value or "")
        elif name == "refonly":
            view.ref_only = sign != "-"
        elif name in _MECHANICS or name in _BUILD_TYPE:
            continue
        elif name == "define":
            for d in _split_list(value or ""):
                defines[d] = None
        elif name in ("reference", "link"):
            for item in _split_list(value or ""):
                item = item.split("=", 1)[1] if "=" in item and not ntpath.splitdrive(item)[0] else item
                references.append(path(item))
                raw_references.append(item.replace("\\", "/"))
        elif name == "analyzer":
            for item in _split_list(value or ""):
                analyzers.append(path(item))
                raw_analyzers.append(item.replace("\\", "/"))
        elif name == "additionalfile":
            additional.extend(path(i) for i in _split_list(value or ""))
        elif name == "analyzerconfig":
            configs.extend(path(i) for i in _split_list(value or ""))
        elif name in ("resource", "linkresource"):
            # /resource:<file>[,<logical name>[,public|private]]
            parts = [p.strip().strip('"') for p in (value or "").split(",")]
            file_path = path(parts[0])
            logical = parts[1] if len(parts) > 1 and parts[1] else ntpath.basename(
                parts[0].replace("/", "\\"))
            resources.append((logical, file_path))
        elif name == "nowarn":
            for w in _split_list(value or ""):
                nowarn[warning_id(w)] = None
        elif name == "warnaserror":
            if value is None:
                all_warnings_as_errors = sign != "-"
            else:
                bucket = warn_not_as_error if sign == "-" else warn_as_error
                other = warn_as_error if sign == "-" else warn_not_as_error
                for w in _split_list(value):
                    bucket[warning_id(w)] = None
                    other.pop(warning_id(w), None)
        elif name == "nullable":
            values["nullable"] = (value or ("disable" if sign == "-" else "enable")).lower()
        elif name == "langversion":
            values["langversion"] = _langversion(value or "default")
        elif name == "target":
            values["target"] = (value or "exe").lower()
        elif name in _BOOLEAN_DEFAULT:
            booleans[name] = sign != "-"
        elif name in _VALUE_DEFAULT:
            values[name] = (value or "").strip('"').lower()
        elif name == "doc":
            options.append("/doc")          # the path is mechanics; that it is on is not
        elif name == "features":
            options.extend("/features:" + f for f in _split_list(value or ""))
        elif name in _PATH_VALUE:
            options.append("/%s:%s" % (name, path(value or "")))
        elif value is None:
            options.append("/" + name + sign)
        else:
            options.append("/%s%s:%s" % (name, sign, value.strip('"')))

    view.target = values.get("target", "exe")
    for name, on in booleans.items():
        if on != _BOOLEAN_DEFAULT[name]:
            options.append("/" + name if on else "/" + name + "-")
    for name, value in values.items():
        if _VALUE_DEFAULT[name] is None or value != _VALUE_DEFAULT[name]:
            options.append("/%s:%s" % (name, value))
    if "target" not in values:
        options.append("/target:exe")
    options.extend("/nowarn:" + w for w in nowarn)
    if all_warnings_as_errors:
        options.append("/warnaserror")
    options.extend("/warnaserror:" + w for w in warn_as_error)
    options.extend("/warnaserror-:" + w for w in warn_not_as_error)

    view.sources = tuple(sorted(set(sources), key=str.lower))
    view.defines = tuple(sorted(defines))
    view.options = tuple(sorted(set(options)))
    view.references = tuple(references)
    view.raw_references = tuple(raw_references)
    view.analyzers = tuple(analyzers)
    view.raw_analyzers = tuple(raw_analyzers)
    view.additional_files = tuple(sorted(set(additional), key=str.lower))
    view.analyzer_configs = tuple(sorted(set(configs), key=str.lower))
    view.resources = tuple(sorted(set(resources)))
    return view


def default_family(option: str) -> Optional[str]:
    """For a canonical option that states a NON-default value of a setting the
    compiler has a default for: the setting's name. A side that says nothing
    about such a setting is not "missing a flag"; it has the default. So an
    option like this on one side only is a difference whichever side it is on
    -- it cannot be waved through as an extra flag."""
    name = option[1:].partition(":")[0].rstrip("-").lower()
    if name in _BOOLEAN_DEFAULT or (name in _VALUE_DEFAULT and _VALUE_DEFAULT[name] is not None):
        return name
    return None


def assembly_identity(path: str) -> str:
    """What a reference or analyzer IS, across builds: its file name. A compile
    cannot reference two assemblies of the same simple name, so the name is a
    sound key within one command line."""
    return _basename(path)


def package_of(path: str) -> Optional[Tuple[str, str]]:
    """(package id lower-cased, version) of a file inside a NuGet package or
    SDK pack directory, on either side. None for anything else (a project
    output, a loose DLL)."""
    p = "/" + path.replace("\\", "/").lstrip("/")
    for pattern in _PKG_PATTERNS:
        m = pattern.search(p)
        if m:
            return m.group("id").lower(), m.group("ver")
    return None


def is_framework_pack(package_id: str) -> bool:
    return package_id.lower().startswith(FRAMEWORK_PACKS)


def dotnet_facts(command_lines: Iterable[Sequence[str]]) -> Dict[str, str]:
    """Toolchain facts of a set of csc command lines, for CanonicalModel.toolchain:
    `dotnet_sdk_version` (when the compiler is an SDK's) and `pack:<id>` for
    each framework reference pack, with every version in use (a solution can
    target .NET 8 and .NET 10 at once)."""
    sdks: Dict[str, None] = {}
    packs: Dict[str, Dict[str, None]] = {}
    for args in command_lines:
        i = compiler_index(args)
        if i < 0:
            continue
        version = sdk_version(args[i])
        if version:
            sdks[version] = None
        seen = set()
        for tok in args[i + 1:]:
            parsed = _option(tok)
            if parsed is None or parsed[0] not in ("reference", "link") or not parsed[1]:
                continue
            pkg = package_of(parsed[1].strip('"'))
            if pkg and is_framework_pack(pkg[0]) and pkg not in seen:
                seen.add(pkg)
                packs.setdefault(pkg[0], {})[pkg[1]] = None
    facts = {"pack:" + pid: ",".join(sorted(v)) for pid, v in packs.items()}
    if sdks:
        facts["dotnet_sdk_version"] = ",".join(sorted(sdks))
    return facts


def sdk_version(compiler: str) -> str:
    """.NET SDK version from a compiler path such as
    `.../sdk/10.0.401/Roslyn/bincore/csc.dll` ('' if it is not an SDK path)."""
    m = re.search(r"[\\/]sdk[\\/](\d+\.\d+\.\d+[0-9A-Za-z.\-]*)[\\/]Roslyn[\\/]", compiler, re.I)
    return m.group(1) if m else ""
