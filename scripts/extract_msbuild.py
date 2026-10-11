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

"""Extract a CanonicalModel from an MSBuild build.

MSBuild has no action graph to query, so -- like the Maven frontend -- this one
CAPTURES what a real build ran instead of synthesizing it from project files.
The source is a binary log:

    msbuild <solution> <the project's usual arguments> -bl:msbuild.binlog
    python3 extract_msbuild.py msbuild.binlog <repo_root> model.msbuild.json \\
        --report msbuild.report.json

A binlog alone is not enough. It has a command line only for tools that RAN;
a compile MSBuild judged up to date logs nothing. Those are recovered from the
project's tracking logs (see msbuild_tlog.py), and every project is checked for
completeness: evaluated source items versus recovered compile actions. Anything
unrecovered is listed and fails the run, rather than quietly producing a model
of a third of the build.

What becomes what:

  project instance            -> Target   (one per project file x
                                           Configuration|Platform x framework)
  CL task, per source         -> CppCompile  (argv = tool + shared flags + source,
                                              cwd = project directory)
  Link / LIB task             -> CppLink / CppArchive
  Csc task                    -> CSharpCompile
  RC / MIDL task              -> ResourceCompile / Midl   (recorded, not diffed)

Everything else worth knowing but not diffed -- how the build was configured,
which toolchain it resolved, where each external path came from, what
non-compile steps ran, how tests are hosted -- goes to the report, which is
what a BUILD-file author reads instead of megabytes of argv.
"""

from __future__ import annotations

import argparse
import glob
import ntpath
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import msbuild_csharp as csharp
import msbuild_events as events
import msbuild_origins as origins_mod
import canonicalize_csc as csc_dialect
import wincmdline
from canonicalize_msvc import (canonicalize_cl_flags, canonicalize_link_flags,
                               cl_include_dirs, lib_identity, link_libraries,
                               link_output, norm_path)
from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TargetRole)
from msbuild_origins import Origins, is_abs, win_norm
from msbuild_tlog import TlogDir
from serialize import dump_model

REPORT_SCHEMA = 1

# Files MSBuild builds that are projects in their own right. Solution
# metaprojects, restore graphs and stray .targets entry points only contribute
# ordering edges.
_PROJECT_EXTS = (".vcxproj", ".csproj", ".vbproj", ".fsproj", ".wapproj", ".wixproj",
                 ".vdproj", ".sfproj", ".proj")
_PACKAGE_EXTS = (".wapproj", ".wixproj", ".vdproj", ".sfproj")

_CPP_KIND = {"application": TargetKind.EXECUTABLE, "dynamiclibrary": TargetKind.SHARED,
             "staticlibrary": TargetKind.STATIC}
_NET_KIND = {"exe": TargetKind.EXECUTABLE, "winexe": TargetKind.EXECUTABLE,
             "appcontainerexe": TargetKind.EXECUTABLE, "library": TargetKind.STATIC}

_ARCH = {"x64": "x64", "win32": "x86", "x86": "x86", "arm64": "arm64", "arm": "arm",
         "arm64ec": "arm64ec"}

# Options whose value MSBuild writes as a separate argument.
_CL_SPACED = {"/D", "-D", "/U", "-U", "/I", "-I", "/FI", "/AI", "/FU", "/Tp", "/Tc",
              "/external:I"}

_TEST_PACKAGES = (("mstest", "mstest"), ("microsoft.testing.platform", "mstest"),
                  ("xunit", "xunit"), ("nunit", "nunit"))
_TEST_NAME = re.compile(r"(unit|ui|fuzz|fuzzing|e2e)?tests?$|tests?[.\-_]|fuzzing$", re.IGNORECASE)


# ---- small path helpers (all ntpath: host independent) -------------------------

def _join(base: str, path: str) -> str:
    """Absolute, normalized form of `path` taken relative to `base`."""
    path = path.strip().strip('"')
    if not path:
        return path
    if is_abs(path) or not base:
        return win_norm(path)
    return win_norm(ntpath.join(base, path))


def _repo_rel(path: str, repo_root: str) -> str:
    """Repo-relative POSIX path, or the absolute path with forward slashes."""
    norm = win_norm(path)
    root = win_norm(repo_root)
    if norm.lower() == root.lower():
        return "."
    if norm.lower().startswith(root.lower() + "\\"):
        return norm[len(root) + 1:].replace("\\", "/")
    return norm.replace("\\", "/")


class _TrueCase:
    """Restore the on-disk case of a path. Tracking logs upper-case every path;
    Windows does not care, but a BUILD file author reading `SRC\\FOO\\PUBLIC`
    does. Best effort: a path that is not on this machine is returned as is."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._dirs: Dict[str, Dict[str, str]] = {}

    def _names(self, directory: str) -> Dict[str, str]:
        key = directory.lower()
        if key not in self._dirs:
            try:
                self._dirs[key] = {n.lower(): n for n in os.listdir(directory)}
            except OSError:
                self._dirs[key] = {}
        return self._dirs[key]

    def path(self, absolute: str) -> str:
        if not self.enabled or not is_abs(absolute):
            return absolute
        drive, rest = ntpath.splitdrive(win_norm(absolute))
        parts = [p for p in rest.split("\\") if p]
        root = (drive.upper() if len(drive) == 2 else drive) + "\\"
        current = root
        done: List[str] = []
        for i, part in enumerate(parts):
            real = self._names(current).get(part.lower())
            if real is None:
                done.extend(parts[i:])      # not on disk from here down
                break
            done.append(real)
            current = ntpath.join(current, real)
        return ntpath.join(root, *done) if done else root


_DRIVE_PATH = re.compile(r"[A-Za-z]:\\")
# Joined-value options whose value is a path that may be relative.
_PATH_OPTIONS = ("/external:I", "/I", "/Fo", "/Fd", "/Fp", "/Fe", "/OUT:", "/IMPLIB:",
                 "/PDB:", "/DEF:", "/LIBPATH:", "/LTCGOUT:", "/fo")


def _restore_token_case(tok: str, truecase: _TrueCase, cwd: str = "") -> str:
    """Restore the on-disk case of the path in a tracking-log token:
    `/IC:\\FOO\\BAR` -> `/IC:\\Foo\\Bar`, and (given the working directory)
    `/IX64\\RELEASE\\` -> `/Ix64\\Release\\`. Anything else is returned as is."""
    m = _DRIVE_PATH.search(tok)
    if m:
        path = tok[m.start():]
        trailing = "\\" if path.endswith("\\") else ""
        return tok[:m.start()] + truecase.path(path) + trailing
    if not cwd or not truecase.enabled:
        return tok
    prefix = ""
    if tok.startswith(("/", "-")):
        prefix = next((o for o in _PATH_OPTIONS if tok[1:].startswith(o[1:])), None)
        if prefix is None:
            return tok
        prefix = tok[:len(prefix)]
    rel = tok[len(prefix):]
    if not rel or rel.startswith(("\\", "/")):
        return tok
    trailing = "\\" if rel.endswith("\\") else ""
    base = truecase.path(cwd)
    restored = truecase.path(win_norm(ntpath.join(cwd, rel)))
    if restored.lower().startswith(base.lower() + "\\"):
        return prefix + restored[len(base) + 1:] + trailing
    # Outside the working directory (`/I..\UTILS`): keep it relative, the way
    # the project wrote it.
    try:
        return prefix + ntpath.relpath(restored, base) + trailing
    except ValueError:              # another drive
        return tok


# ---- projects ---------------------------------------------------------------

@dataclass
class _Group:
    """All instances of one project in one configuration: MSBuild runs a
    project several times (once to build, more to answer queries from
    referencing projects). The work is merged; the evaluation is shared."""
    file: str
    evaluation: events.Evaluation
    instances: List[events.Project] = field(default_factory=list)
    name: str = ""

    def prop(self, name: str) -> str:
        return self.evaluation.props.get(name, "")

    @property
    def dir(self) -> str:
        return win_norm(self.prop("ProjectDir") or ntpath.dirname(self.file))

    @property
    def ext(self) -> str:
        return ntpath.splitext(self.file)[1].lower()

    def tasks(self, *names: str) -> Iterable[events.Task]:
        for inst in self.instances:
            yield from inst.tasks(*names)

    def requested(self) -> bool:
        """Was this configuration of the project asked to BUILD? MSBuild also
        evaluates a project under other global properties just to answer a
        question from a referencing project (its target framework, its output
        path); those evaluations build nothing."""
        return any(_is_build_request(inst) for inst in self.instances)

    def built(self) -> bool:
        """Did this build actually run the project, rather than reuse a result
        or never reach it?"""
        return any(_is_build_request(inst) and inst.targets for inst in self.instances)


_BUILD_TARGETS = {"build", "rebuild", "compile", "publish", "pack"}


def _is_build_request(project: events.Project) -> bool:
    entry = [t.strip().lower() for t in project.entry_targets.split(";") if t.strip()]
    return not entry or any(t in _BUILD_TARGETS for t in entry)


def _identity(ev: events.Evaluation) -> Tuple[str, str, str]:
    g = lambda n: ev.props.get(n, "") or ev.globals.get(n, "")
    return g("Configuration"), g("Platform"), g("TargetFramework")


def _groups(log: events.BuildLog, repo_root: str) -> List[_Group]:
    by_key: Dict[tuple, _Group] = {}
    for proj in log.projects.values():
        ext = ntpath.splitext(proj.file)[1].lower()
        ev = log.evaluation_of(proj)
        if ext not in _PROJECT_EXTS or ev is None or not len(ev.props):
            continue
        # The outer build of a multi-targeted project only dispatches to one
        # inner build per framework; it compiles nothing itself.
        if ev.props.get("TargetFrameworks") and not ev.props.get("TargetFramework"):
            continue
        key = (win_norm(proj.file).lower(),) + _identity(ev)
        group = by_key.get(key)
        if group is None:
            group = by_key[key] = _Group(file=win_norm(proj.file), evaluation=ev)
        group.instances.append(proj)
        # Prefer the evaluation of the instance that did the real work.
        best = max(group.instances, key=lambda p: len(p.targets))
        best_ev = log.evaluation_of(best)
        if best_ev is not None and len(best_ev.props):
            group.evaluation = best_ev
    # Drop configurations that were only queried, unless that is all we have
    # for the file (then it stays visible instead of vanishing).
    requested_files = {g.file.lower() for g in by_key.values() if g.requested()}
    groups = sorted((g for g in by_key.values()
                     if g.requested() or g.file.lower() not in requested_files),
                    key=lambda g: (g.file.lower(), _identity(g.evaluation)))
    _name(groups, repo_root)
    return groups


def _name(groups: List[_Group], repo_root: str) -> None:
    """Target name = project name. A name shared by several groups is
    disambiguated by path (different files), then by whichever of framework,
    configuration or platform tells the same file's groups apart."""
    for g in groups:
        g.name = (g.prop("ProjectName") or g.prop("MSBuildProjectName")
                  or ntpath.splitext(ntpath.basename(g.file))[0])
    by_name: Dict[str, List[_Group]] = {}
    for g in groups:
        by_name.setdefault(g.name.lower(), []).append(g)
    for same in by_name.values():
        if len({g.file.lower() for g in same}) > 1:
            for g in same:
                g.name = ntpath.splitext(_repo_rel(g.file, repo_root))[0]
    by_file: Dict[str, List[_Group]] = {}
    for g in groups:
        by_file.setdefault(g.file.lower(), []).append(g)
    for same in by_file.values():
        if len(same) < 2:
            continue
        identities = [_identity(g.evaluation) for g in same]
        # Use only the parts that differ: (cfg, platform, framework).
        differing = [i for i in range(3) if len({ident[i] for ident in identities}) > 1]
        for g, ident in zip(same, identities):
            parts = [ident[i] for i in (2, 0, 1) if i in differing and ident[i]]
            g.name = "%s[%s]" % (g.name, "|".join(parts) or "|".join(p for p in ident if p))


# ---- compile ------------------------------------------------------------------

def _split_cl(args: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(flags, sources) of a logged CL invocation. MSBuild appends the sources
    after every switch, so they are the trailing positional run."""
    i = len(args)
    while i > 0:
        tok = args[i - 1]
        if tok.startswith(("/", "-")):
            break
        if i >= 2 and args[i - 2] in _CL_SPACED:
            break               # the value of a spaced option, not a source
        i -= 1
    return list(args[:i]), list(args[i:])


def _excluded(item: events.Item) -> bool:
    return item.get("ExcludedFromBuild").strip().lower() == "true"


def _cl_items(g: _Group) -> Dict[str, str]:
    """Sources the project compiles: {upper-cased absolute path: absolute
    path}. Evaluated `ClCompile` items, plus whatever a CL task was actually
    handed (generated sources a target added while running), minus
    `ExcludedFromBuild`.

    Items a target merely ADDED are not expected: with `/std:c++latest` MSBuild
    adds the toolset's `std.ixx` as a candidate and compiles it only if some
    source imports it."""
    out: Dict[str, str] = {}
    items = list(g.evaluation.items.get("ClCompile", []))
    for inst in g.instances:
        for task in inst.tasks("CL"):
            items.extend(task.inputs.get("Sources", []))
    for item in items:
        if _excluded(item) or not item.spec.strip():
            continue
        absolute = _join(g.dir, item.spec)
        out.setdefault(absolute.upper(), absolute)
    return out


def _tlog(g: _Group, task: Optional[events.Task] = None) -> Optional[TlogDir]:
    location = (task.scalar("TrackerLogDirectory") if task else "") or g.prop("TLogLocation")
    return TlogDir(_join(g.dir, location)) if location else None


@dataclass
class _Extracted:
    actions: List[Action] = field(default_factory=list)
    provenance: Counter = field(default_factory=Counter)
    unrecovered: List[str] = field(default_factory=list)
    # C#: how the design-time command line differs from the one the build
    # really ran (None: not both available; {}: identical).
    design_time_check: Optional[dict] = None


def _compile_cpp(g: _Group, use_tlogs: bool, truecase: _TrueCase) -> _Extracted:
    out = _Extracted()
    by_source: Dict[str, Action] = {}
    tlog_dir: Optional[TlogDir] = None
    # Sources a CL task that SUCCEEDED was handed in this build. One of these
    # with no command line was skipped because MSBuild compared the command it
    # would run against the tracking log and found them equal -- which is what
    # makes that tracking-log entry trustworthy.
    checked: Set[str] = set()
    for task in g.tasks("CL"):
        tlog_dir = tlog_dir or _tlog(g, task)
        if task.succeeded:
            checked.update(_join(g.dir, i.spec).upper() for i in task.inputs.get("Sources", []))
        for cmd in task.command_lines:
            tool, args = wincmdline.split_tool(cmd)
            flags, sources = _split_cl(args)
            for src in sources:
                absolute = _join(g.dir, src)
                by_source[absolute.upper()] = Action(
                    mnemonic="CppCompile", arguments=(tool, *flags, src),
                    inputs=(absolute,), cwd=g.dir)
    out.provenance["binlog"] = len(by_source)

    wanted = _cl_items(g)
    missing = [k for k in wanted if k not in by_source]
    if missing and use_tlogs:
        tlog_dir = tlog_dir or _tlog(g)
        commands = tlog_dir.commands("CL") if tlog_dir and tlog_dir.exists() else {}
        true_names = tlog_dir.items() if commands else {}
        for key in missing:
            cmd = commands.get(key)
            if not cmd:
                continue
            _, args = wincmdline.split_tool(cmd)
            flags, _sources = _split_cl(args)
            flags = [_restore_token_case(f, truecase, g.dir) for f in flags]
            absolute = wanted.get(key) or true_names.get(key) or key
            by_source[key] = Action(
                mnemonic="CppCompile", arguments=("cl.exe", *flags, absolute),
                inputs=(absolute,), cwd=g.dir)
            out.provenance["tlog" if key in checked else "tlog_unverified"] += 1
    out.unrecovered = sorted(wanted[k] for k in wanted if k not in by_source)
    # Stable order: as the project lists its sources, then anything extra.
    ordered = [k for k in wanted if k in by_source] + sorted(k for k in by_source if k not in wanted)
    out.actions = [by_source[k] for k in ordered]
    return out


def _tool_actions(g: _Group, task_name: str, tlog_tool: str, mnemonic: str,
                  use_tlogs: bool, truecase: _TrueCase) -> List[Action]:
    """RC and MIDL: one action per logged command line, else per tlog entry."""
    actions: List[Action] = []
    ran = False
    for task in g.tasks(task_name):
        ran = True
        for cmd in task.command_lines:
            tool, args = wincmdline.split_tool(cmd)
            actions.append(Action(mnemonic=mnemonic, arguments=(tool, *args), cwd=g.dir))
    if not actions and use_tlogs and (ran or not g.built()):
        tlog_dir = _tlog(g)
        if tlog_dir and tlog_dir.exists():
            seen: Set[str] = set()
            for cmd in tlog_dir.commands(tlog_tool).values():
                if cmd in seen:
                    continue
                seen.add(cmd)
                _, args = wincmdline.split_tool(cmd)
                args = [_restore_token_case(a, truecase, g.dir) for a in args]
                actions.append(Action(mnemonic=mnemonic,
                                      arguments=(tlog_tool + ".exe", *args), cwd=g.dir))
    return actions


def _link(g: _Group, kind: TargetKind, use_tlogs: bool,
          truecase: _TrueCase) -> Tuple[Optional[Action], str]:
    """The project's link or archive action and where it came from."""
    verified = False
    for task_name, mnemonic in (("Link", "CppLink"), ("LIB", "CppArchive")):
        for task in g.tasks(task_name):
            # Ran, succeeded, logged no command: the tracker found it current.
            verified = verified or bool(task.succeeded)
            for cmd in task.command_lines:
                tool, args = wincmdline.split_tool(cmd)
                output = link_output(args)
                return (Action(mnemonic=mnemonic, arguments=(tool, *args), cwd=g.dir,
                               outputs=(_join(g.dir, output),) if output else ()), "binlog")
    if not use_tlogs or kind not in (TargetKind.EXECUTABLE, TargetKind.SHARED, TargetKind.STATIC):
        return None, ""
    tlog_dir = _tlog(g)
    if not tlog_dir or not tlog_dir.exists():
        return None, ""
    tool, mnemonic = ("lib", "CppArchive") if kind == TargetKind.STATIC else ("link", "CppLink")
    commands = tlog_dir.commands(tool)
    if not commands:
        return None, ""
    cmd = next(iter(commands.values()))
    _, args = wincmdline.split_tool(cmd)
    args = [_restore_token_case(a, truecase, g.dir) for a in args]
    output = link_output(args)
    return (Action(mnemonic=mnemonic, arguments=(tool + ".exe", *args), cwd=g.dir,
                   outputs=(_join(g.dir, output),) if output else ()),
            "tlog" if verified else "tlog_unverified")


MANAGED_EXTS = (".csproj", ".vbproj", ".fsproj")


def _instance_key(project_file: str, global_properties: Dict[str, str]) -> tuple:
    """What identifies one build of a managed project across two logs."""
    g = {k.lower(): v for k, v in global_properties.items()}
    return (win_norm(project_file).lower(), g.get("configuration", "").lower(),
            g.get("platform", "").lower(), g.get("targetframework", "").lower())


def _build_instance(g: _Group) -> Optional[events.Project]:
    return next((i for i in g.instances if _is_build_request(i)), None)


def design_time_instances(log: events.BuildLog, repo_root: str) -> List[Tuple[str, Dict[str, str]]]:
    """(project file, global properties) of every managed project instance the
    build ran: the input of `msbuild_events.design_time_compile`."""
    out = []
    for g in _groups(log, repo_root):
        inst = _build_instance(g)
        if g.ext in MANAGED_EXTS and inst is not None:
            out.append((g.file, dict(inst.globals)))
    return out


def _design_time_commands(log: events.BuildLog) -> Dict[tuple, Tuple[str, ...]]:
    """instance key -> csc argv, from a design-time log. The arguments come
    from the Csc task's `CommandLineArgs` output (one item per argument), so
    nothing is re-tokenized."""
    out: Dict[tuple, Tuple[str, ...]] = {}
    for project in log.projects.values():
        for task in project.tasks("Csc"):
            items = task.outputs.get("CommandLineArgs")
            if not items:
                continue
            tool = wincmdline.split_tool(task.command_lines[0])[0] if task.command_lines else ""
            out[_instance_key(project.file, project.globals)] = (
                tool or "csc.exe", *_without_design_time_artifacts(i.spec for i in items))
    return out


# midl switches that say where output goes or that the WinRT metadata step
# always has; everything else is the project's choice.
_MIDL_FIXED = {"/winrt", "/nologo", "/notlb", "/nomidl"}
_MIDL_FIXED_WITH_VALUE = {"/metadata_dir", "/winmd", "/h", "/dlldata", "/iid", "/proxy",
                          "/client", "/server", "/out", "/tlb"}
_RSP_QUOTED = re.compile(r'"([^"]*)"|(\S+)')


def _winrt_metadata(target: Target, steps: List[dict], repo_root: str) -> Optional[dict]:
    """How a project turns MIDL 3 into its `.winmd`: the `midl /winrt` action,
    the `mdmerge` step after it, and where the result is copied to. This is
    the head of the WinRT chain: every projection (C++/WinRT, C#/WinRT) is
    generated from the merged file, and other projects find it by path."""
    midl = [a for a in target.actions if a.mnemonic == "Midl"
            and any(x.lower() == "/winrt" for x in a.arguments)]
    if not midl:
        return None
    idl: List[str] = []
    per_action: List[List[str]] = []
    for action in midl:
        args = list(action.arguments[1:])
        options: List[str] = []
        per_action.append(options)
        i = 0
        while i < len(args):
            tok, low = args[i], args[i].lower()
            if low in _MIDL_FIXED_WITH_VALUE:
                i += 2
                continue
            if low in _MIDL_FIXED or tok.startswith("@"):
                i += 1
                continue
            if low.endswith(".idl") and not tok.startswith(("/", "-")):
                idl.append(_repo_rel(_join(action.cwd, tok), repo_root))
            else:
                options.append(tok)
            i += 1
    # One midl run per .idl, normally all with the project's one set of options.
    out: dict = {"idl": sorted(set(idl)), "midl_options": per_action[0]}
    if any(o != per_action[0] for o in per_action[1:]):
        out["midl_options_vary"] = True
    references: List[str] = []
    merged_dir = ""
    for step in steps:
        if step.get("kind") != "write":
            continue
        text = " ".join(step.get("lines", []))
        tokens = [a or b for a, b in _RSP_QUOTED.findall(text)]
        if step["file"].lower().endswith("midlrt.rsp"):
            for flag, value in zip(tokens, tokens[1:]):
                if flag.lower() == "/reference" and is_abs(value):
                    rel = _repo_rel(win_norm(value), repo_root)
                    if not is_abs(rel):
                        references.append(rel)
        elif step["file"].lower().endswith("mdmerge.rsp"):
            # mdmerge runs where midl ran, in the project directory. The
            # intermediate directory the response file is in can be anywhere
            # below it (`x64\Release\`, `obj\x64\Release\`).
            base = ntpath.dirname(win_norm(_join(repo_root, step["file"])))
            project_dir = win_norm(midl[0].cwd) if midl[0].cwd \
                else ntpath.dirname(ntpath.dirname(base))
            for flag, value in zip(tokens, tokens[1:]):
                if flag.lower() == "-o":
                    merged_dir = _repo_rel(_join(project_dir, value), repo_root)
            depth = next((t[3:] for t in tokens if t.lower().startswith("-n:")), "")
            if depth:
                out["namespace_depth"] = int(depth)
    if references:
        out["references"] = sorted(set(references))
    copies: List[str] = []
    for step in steps:
        if step.get("kind") != "copy":
            continue
        sources = step.get("from", [])
        destinations = step.get("to", [])
        if isinstance(destinations, str):
            continue
        for source, destination in zip(sources, destinations):
            if source.lower().endswith(".winmd") and merged_dir \
                    and source.lower().startswith(merged_dir.lower() + "/"):
                out["winmd"] = source
                copies.append(destination)
    if copies:
        out["copies"] = sorted(set(copies))
    return out


def _managed_owner(path: str, outputs: Dict[str, List[_Group]], asking: str) -> Optional[str]:
    """Name of the managed target whose output `path` is, or None. A project
    that multi-targets writes one assembly per framework under the same file
    name; the framework directory in the path says which one."""
    candidates = [g for g in outputs.get(ntpath.basename(path.replace("/", "\\")).lower(), [])
                  if g.name != asking]
    if not candidates:
        return None
    probe = "\\" + win_norm(path).lower() + "\\"
    for g in candidates:
        tfm = g.prop("TargetFramework").lower()
        if tfm and ("\\%s\\" % tfm) in probe:
            return g.name
    return candidates[0].name


def _without_design_time_artifacts(args: Iterable[str]) -> List[str]:
    """A design-time build suppresses CS2008 ("no source files specified") so
    that an empty project does not error in the IDE. It is the one thing the
    pass adds to every command line that a real build does not have."""
    out = []
    for tok in args:
        if tok[:8].lower() in ("/nowarn:", "-nowarn:"):
            ids = [w for w in re.split(r"[;,]", tok[8:]) if w.strip() and w.strip() != "2008"]
            if not ids:
                continue
            tok = tok[:8] + ",".join(ids)
        out.append(tok)
    return out


def _resource_sources(project_dir: str, tasks: Iterable[events.Task]) -> Dict[str, str]:
    """generated `.resources` path (absolute, lower-case) -> the `.resx` it
    was generated from (absolute), from GenerateResource tasks. The compiler
    only sees the generated file; a BUILD author needs the source."""
    out: Dict[str, str] = {}
    for task in tasks:
        sources = task.inputs.get("Sources", [])
        outputs = task.inputs.get("OutputResources", [])
        for source, output in zip(sources, outputs):
            out[_join(project_dir, output.spec).lower()] = _join(project_dir, source.spec)
    return out


def _design_time_resources(log: events.BuildLog) -> Dict[tuple, Dict[str, str]]:
    out: Dict[tuple, Dict[str, str]] = {}
    for project in log.projects.values():
        found = _resource_sources(ntpath.dirname(win_norm(project.file)),
                                  project.tasks("GenerateResource"))
        if found:
            out.setdefault(_instance_key(project.file, project.globals), {}).update(found)
    return out


def _csc_mismatch(real: Sequence[str], computed: Sequence[str], repo_root: str,
                  cwd: str) -> dict:
    """Fields in which a design-time command line differs from the one the
    build ran, canonically (so quoting and order do not count)."""
    a = csc_dialect.parse(real, repo_root, cwd=cwd)
    b = csc_dialect.parse(computed, repo_root, cwd=cwd)
    out: dict = {}
    for name in ("sources", "defines", "options", "references", "analyzers",
                 "additional_files", "analyzer_configs", "resources"):
        left = {str(x).lower() for x in getattr(a, name)}
        right = {str(x).lower() for x in getattr(b, name)}
        if left != right:
            out[name] = {"build_only": sorted(left - right)[:20],
                         "design_time_only": sorted(right - left)[:20]}
    return out


def _compile_csharp(g: _Group, design_time: Optional[Dict[tuple, Tuple[str, ...]]] = None
                    ) -> _Extracted:
    out = _Extracted()
    for task in g.tasks("Csc"):
        for cmd in task.command_lines:
            tool, args = wincmdline.split_tool(cmd)
            out.actions.append(Action(mnemonic="CSharpCompile", arguments=(tool, *args),
                                      cwd=g.dir))
            out.provenance["binlog"] += 1
    inst = _build_instance(g)
    computed = (design_time or {}).get(_instance_key(g.file, inst.globals)) if inst else None
    if computed and out.actions:
        out.design_time_check = _csc_mismatch(out.actions[0].arguments, computed, "", g.dir)
    elif computed:
        # The build skipped this compile as up to date; this is the command
        # line MSBuild computes for it now, from the same global properties.
        out.actions.append(Action(mnemonic="CSharpCompile", arguments=computed, cwd=g.dir))
        out.provenance["design_time"] += 1
    if not out.actions:
        # Compile skipped as up to date: MSBuild still logged the target's
        # declared inputs, which include every source.
        for inst in g.instances:
            for skipped in inst.skipped:
                if skipped.name == "CoreCompile" and skipped.reason == "OutputsUpToDate":
                    sources = tuple(_join(g.dir, s) for s in skipped.inputs
                                    if s.lower().endswith(".cs"))
                    if sources:
                        out.actions.append(Action(
                            mnemonic="CSharpCompile", inputs=sources, cwd=g.dir,
                            outputs=tuple(_join(g.dir, o) for o in skipped.outputs)))
                        out.provenance["skipped_target"] += 1
                        return out
        wanted = [i for i in g.evaluation.items.get("Compile", []) if i.spec.strip()]
        if wanted:
            out.unrecovered = sorted(_join(g.dir, i.spec) for i in wanted)
    return out


# ---- roles ------------------------------------------------------------------

def _test_evidence(g: _Group, compile_actions: List[Action]) -> Tuple[str, str]:
    """(framework, evidence) if the project is a test, else ('', '')."""
    if g.prop("IsTestProject").lower() == "true":
        framework = "dotnet-test"
        for item in g.evaluation.items.get("PackageReference", []):
            for prefix, name in _TEST_PACKAGES:
                if item.spec.lower().startswith(prefix):
                    framework = name
        return framework, "IsTestProject=true"
    for item in g.evaluation.items.get("PackageReference", []):
        low = item.spec.lower()
        if low == "microsoft.net.test.sdk":
            return "dotnet-test", "PackageReference " + item.spec
        for prefix, name in _TEST_PACKAGES:
            if low.startswith(prefix):
                return name, "PackageReference " + item.spec
    for action in compile_actions[:3]:
        joined = " ".join(action.arguments).lower()
        if "\\unittest\\include" in joined or "/unittest/include" in joined:
            return "cppunittest", "CppUnitTest include directory on the compile line"
    if g.prop("EnableFuzzer").lower() == "true":
        return "libfuzzer", "EnableFuzzer=true"
    if _TEST_NAME.search(g.name):
        return "unknown", "project name"
    return "", ""


def _kind(g: _Group) -> TargetKind:
    if g.ext == ".vcxproj":
        return _CPP_KIND.get(g.prop("ConfigurationType").lower(), TargetKind.UNKNOWN)
    if g.ext in (".csproj", ".vbproj", ".fsproj"):
        return _NET_KIND.get(g.prop("OutputType").lower(), TargetKind.UNKNOWN)
    return TargetKind.UNKNOWN


# ---- dependencies -----------------------------------------------------------------

def _library_search(name: str, dirs: Sequence[str], fs: bool) -> str:
    if not fs:
        return ""
    for d in dirs:
        candidate = ntpath.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _resolve_libraries(g: _Group, link: Action, producers: Dict[str, str],
                       origins: Origins, fs: bool,
                       truecase: _TrueCase) -> Tuple[List[Dependency], List[dict]]:
    """Turn a link command's library inputs into dependencies.

    Returns (deps, offered). A library named explicitly is a dependency. A
    WILDCARD (`...\\lib\\*.lib`, which vcpkg's MSBuild integration adds to every
    link) names nothing: the linker takes what it needs. Those are reported as
    "offered" and are not dependencies -- treating them as such would demand
    that every binary link every installed library.
    """
    deps: List[Dependency] = []
    offered: List[dict] = []
    seen: Set[Tuple[str, bool]] = set()
    args = list(link.arguments)

    def add(dep: Dependency) -> None:
        if (dep.name, dep.external) not in seen:
            seen.add((dep.name, dep.external))
            deps.append(dep)

    search = [_join(g.dir, tok.split(":", 1)[1])
              for tok in args if tok.upper().startswith(("/LIBPATH:", "-LIBPATH:"))]
    search += [d for d in (p.strip() for p in g.prop("LibraryPath").split(";")) if d and is_abs(d)]

    read_paths: Dict[str, str] = {}
    tlog_dir = _tlog(g)
    if fs and tlog_dir and tlog_dir.exists():
        for tool in ("link", "Lib-link"):
            for files in tlog_dir.reads(tool).values():
                for f in files:
                    if f.lower().endswith(".lib"):
                        read_paths.setdefault(ntpath.basename(f).lower(), truecase.path(f))

    for lib in link_libraries(args):
        if "*" in lib or "?" in lib:
            pattern = _join(g.dir, lib)
            matches = sorted(glob.glob(pattern)) if fs else []
            c = origins.classify(ntpath.dirname(pattern))
            offered.append({"pattern": c.path + "/" + ntpath.basename(pattern),
                            "origin": c.kind,
                            "libraries": [ntpath.basename(m) for m in matches]})
            continue
        if "\\" in lib or "/" in lib or is_abs(lib):
            absolute = _join(g.dir, lib)
            producer = producers.get(absolute.lower())
            if producer and producer != g.name:
                add(Dependency(producer, external=False))
                continue
            if producer == g.name:
                continue
        else:
            absolute = read_paths.get(lib.lower()) or _library_search(lib, search, fs)
        if absolute:
            c = origins.note(absolute, "lib", g.name)
            if c.kind in (origins_mod.REPO, origins_mod.GENERATED):
                # A prebuilt or path-referenced in-repo library no project in
                # this build produced.
                add(Dependency(lib_identity(lib), external=True, origin=c.kind))
            else:
                version = c.root.version if c.root else ""
                add(Dependency(lib_identity(lib), external=True, origin=c.kind, version=version))
        else:
            add(Dependency(lib_identity(lib), external=True))
    return deps, offered


# ---- the report -----------------------------------------------------------------

def _digest(compile_actions: List[Action], repo_root: str,
            path_map: Sequence[Tuple[str, str]]) -> dict:
    """What a BUILD author needs from N compile command lines.

    A project compiles almost all of its sources with one flag set and a few
    with another (a C file in a C++ project, a file that opts out of a
    warning). So: group the sources by identical canonical (defines, includes,
    flags) and report the groups, largest first. The first is the project's
    flag set; each other one also says how it differs from the first.
    """
    sources_of: Dict[tuple, List[str]] = {}
    spelling: Dict[tuple, Tuple[str, ...]] = {}
    for action in compile_actions:
        defines, includes, flags = canonicalize_cl_flags(
            action.arguments, repo_root, cwd=action.cwd, path_map=path_map)
        # Paths compare case-insensitively on Windows, so a tracking-log entry
        # (upper-cased) must land in the same group as its true-case twin.
        key = (defines, tuple(i.lower() for i in includes), flags)
        sources_of.setdefault(key, []).append(_repo_rel(action.inputs[0], repo_root))
        spelling.setdefault(key, includes)
    groups = sorted(sources_of.items(), key=lambda kv: (-len(kv[1]), kv[1]))
    if not groups:
        return {}
    out = []
    main_defines, main_includes, main_flags = (
        set(groups[0][0][0]), groups[0][0][1], set(groups[0][0][2]))
    for index, (key, sources) in enumerate(groups):
        defines, includes_low, flags = key
        includes = spelling[key]
        entry: dict = {"sources": sources, "defines": list(defines),
                       "includes": list(includes), "flags": list(flags)}
        if index:
            delta: dict = {}
            for label, mine, base in (("defines", set(defines), main_defines),
                                      ("flags", set(flags), main_flags)):
                if mine - base:
                    delta.setdefault("added", {})[label] = sorted(mine - base)
                if base - mine:
                    delta.setdefault("removed", {})[label] = sorted(base - mine)
            added = [i for i, low in zip(includes, includes_low) if low not in main_includes]
            removed = [i for i in spelling[groups[0][0]] if i.lower() not in includes_low]
            if added:
                delta.setdefault("added", {})["includes"] = added
            if removed:
                delta.setdefault("removed", {})["includes"] = removed
            entry["differs_from_first"] = delta
        out.append(entry)
    return {"variants": out}


def _external_headers(g: _Group, compile_actions: List[Action], repo_root: str,
                      path_map: Sequence[Tuple[str, str]]) -> dict:
    """How the build treats headers it does not own. `/external:W0` with
    `/external:anglebrackets` silences warnings in every `<...>` include, and
    the directories in the ExternalIncludePath property (handed to the compiler
    through the EXTERNAL_INCLUDE environment variable, never on the command
    line) are external however they are included.

    These options do not change what is built, so the differ ignores them --
    but a project that also has `/WX` does not compile without them: the first
    warning in a third-party header is an error."""
    flags = sorted({tok.replace("-", "/", 1) if tok.startswith("-") else tok
                    for a in compile_actions for tok in a.arguments
                    if tok[1:].startswith("external:") and not tok[1:].startswith("external:I")})
    if not flags:
        return {}
    dirs = [norm_path(win_norm(d), repo_root, "", path_map)
            for d in g.prop("ExternalIncludePath").split(";") if d.strip() and is_abs(d.strip())]
    out: dict = {"flags": flags}
    if dirs:
        out["env_dirs"] = _one_spelling(dirs)
    return out


def _dead_include_dirs(compile_actions: List[Action], repo_root: str,
                       path_map: Sequence[Tuple[str, str]]) -> List[str]:
    """Include directories on the compile line that do not exist on disk, as
    the digest spells them. A project can carry one for years (a path from an
    older Visual Studio layout, a dependency that moved): the compiler skips
    it silently. It contributes nothing, so the Bazel side needs no
    counterpart -- but only a look at the disk can tell it from a real one."""
    dead: Dict[str, str] = {}
    for action in compile_actions:
        for directory in cl_include_dirs(action.arguments):
            absolute = _join(action.cwd, directory)
            if absolute.lower() in dead or os.path.isdir(absolute):
                continue
            dead[absolute.lower()] = norm_path(absolute, repo_root, "", path_map)
    return sorted(dead.values(), key=str.lower)


# What a compile reads back from its own build directory without it being an
# input anyone declares.
_BYPRODUCT_EXTS = (".pch", ".pchast", ".pdb", ".obj", ".idb", ".ipdb", ".iobj", ".ifc",
                   ".res", ".tlog")
_PROJECT_FILE_EXTS = (".vcxproj", ".vcxitems", ".csproj", ".vbproj", ".fsproj")
_MAX_EXAMPLES = 10
# A directory with at most this many generated files is listed in full: few
# enough to be the output of a project's own step rather than a projection of
# a whole SDK.
_MAX_LISTED = 64


def _generated_inputs(g: _Group, groups: List[_Group], origins: Origins, repo_root: str,
                      truecase: _TrueCase, compile_actions: List[Action]) -> List[dict]:
    """Generated files under the repo that this project's compiles READ (from
    the compiler's read tracking log), grouped by directory.

    A header a build step wrote is an input no project file lists. Under the
    project's own directory it is the output of one of its own steps. Anywhere
    else another project produced it, and unless that project is referenced,
    nothing but build order guarantees the file exists: built on its own this
    project fails, and Bazel needs the producer as an explicit dependency."""
    tlog_dir = _tlog(g)
    if tlog_dir is None or not tlog_dir.exists():
        return []
    sources = {a.inputs[0].upper() for a in compile_actions if a.inputs}
    include_dirs = sorted(
        {win_norm(_join(a.cwd, d)).rstrip("\\").lower()
         for a in compile_actions for d in cl_include_dirs(a.arguments)},
        key=len, reverse=True)
    own = win_norm(g.dir).rstrip("\\").lower()
    by_dir: Dict[str, set] = {}
    seen = set()
    for files in tlog_dir.reads("CL").values():
        for path in files:
            upper = path.upper()
            if upper in seen:
                continue
            seen.add(upper)
            if upper in sources or upper.lower().endswith(_BYPRODUCT_EXTS):
                continue
            norm = win_norm(path)
            if origins.classify(norm).kind != origins_mod.GENERATED:
                continue
            low = norm.lower()
            # Group under the include directory the file was found through. A
            # file of this project is never filed under an include directory
            # ABOVE the project (`/I..\` covers the project itself).
            mine = low.startswith(own + "\\")
            holder = next((d for d in include_dirs if low.startswith(d + "\\")
                           and (not mine or d == own or d.startswith(own + "\\"))),
                          ntpath.dirname(low))
            by_dir.setdefault(holder, set()).add(norm[len(holder) + 1:])

    referenced = {win_norm(_join(g.dir, item.spec)).lower()
                  for item in g.evaluation.items.get("ProjectReference", [])}
    out: List[dict] = []
    for holder in sorted(by_dir):
        names = sorted(truecase.path(ntpath.join(holder, n))[len(holder) + 1:].replace("\\", "/")
                       for n in by_dir[holder])
        entry: dict = {"dir": _repo_rel(truecase.path(holder), repo_root), "files": len(names),
                       "examples": names[:_MAX_EXAMPLES]}
        if len(names) <= _MAX_LISTED:
            entry["names"] = names
        if holder == own or holder.startswith(own + "\\"):
            entry["produced_by"] = "this project"
        else:
            entry["produced_by"] = "another project"
            owner = max((o for o in groups if o is not g and
                         (holder + "\\").startswith(win_norm(o.dir).rstrip("\\").lower() + "\\")),
                        key=lambda o: len(o.dir), default=None)
            project_file = owner.file if owner else _project_file_above(holder, repo_root, truecase)
            if owner:
                entry["target"] = owner.name
            if project_file:
                entry["project"] = _repo_rel(project_file, repo_root)
            entry["referenced"] = bool(project_file) and win_norm(project_file).lower() in referenced
        out.append(entry)
    return out


# Read by the code analysis a compile runs (`/analyze`), not by the translation
# unit.
_ANALYSIS_EXTS = (".ruleset",)


def _tracked_inputs(g: _Group, tool: str, origins: Origins, repo_root: str,
                    truecase: _TrueCase, given: Iterable[str]
                    ) -> Tuple[List[str], Dict[str, dict], List[str]]:
    """What `tool` READ while building this project, from its read tracking log:
    the files of the repository (repo-relative, without `given`, the files named
    on its command lines), what it took from each package directory
    ({token: {files, ports}}; `ports` for a vcpkg tree), and the files it read
    that a build step had written (repo-relative).

    For the compiler this is the only place a C++ build says which headers a
    project needs. No project file lists them as inputs: an include directory is
    the whole declaration, and it usually reaches far outside the project."""
    tlog_dir = _tlog(g)
    if tlog_dir is None or not tlog_dir.exists():
        return [], {}, []
    skip = {win_norm(p).upper() for p in given}
    repo: Set[str] = set()
    generated: Set[str] = set()
    external: Dict[str, dict] = {}
    seen: Set[str] = set()
    for files in tlog_dir.reads(tool).values():
        for path in files:
            norm = win_norm(path)
            upper = norm.upper()
            if upper in seen:
                continue
            seen.add(upper)
            if upper in skip or upper.lower().endswith(_BYPRODUCT_EXTS + _ANALYSIS_EXTS):
                continue
            c = origins.classify(norm)
            if c.kind == origins_mod.REPO:
                repo.add(_repo_rel(truecase.path(norm), repo_root))
            elif c.kind == origins_mod.GENERATED:
                generated.add(_repo_rel(truecase.path(norm), repo_root))
            elif c.root is not None and c.kind in (origins_mod.NUGET, origins_mod.VCPKG):
                entry = external.setdefault(c.root.token, {"files": 0})
                entry["files"] += 1
                if c.kind == origins_mod.VCPKG and c.root.token.startswith("@vcpkg/"):
                    installed = ntpath.dirname(c.root.path)
                    triplet = ntpath.basename(c.root.path)
                    port = origins.vcpkg_file_owners(installed).get(
                        (triplet + "/" + c.rel).lower())
                    if port:
                        entry.setdefault("ports", set()).add(port)
    for entry in external.values():
        if "ports" in entry:
            entry["ports"] = sorted(entry["ports"])
    return sorted(repo, key=str.lower), external, sorted(generated, key=str.lower)


def _resource_scripts(g: _Group, actions: List[Action], origins: Origins, repo_root: str,
                      truecase: _TrueCase, path_map: Sequence[Tuple[str, str]]) -> List[dict]:
    """One entry per resource compile (`rc.exe`): the script, its options, the
    `.res` it writes (a link input) and the files of the repository it read
    (icons, manifests, version headers: nothing lists those either)."""
    out: List[dict] = []
    scripts = []
    for action in actions:
        args = action.arguments[1:]
        if not args:
            continue
        script = _join(action.cwd, args[-1])
        defines: List[str] = []
        includes: List[str] = []
        options: List[str] = []
        output = ""
        i = 0
        while i < len(args) - 1:
            tok = args[i]
            low = tok.lower()
            spaced = low in ("/d", "-d", "/i", "-i", "/fo", "-fo")
            value = args[i + 1] if spaced and i + 1 < len(args) - 1 else tok[2:]
            if low[:2] in ("/d", "-d") and (spaced or len(tok) > 2):
                defines.append(value)
            elif low[:2] in ("/i", "-i") and (spaced or len(tok) > 2):
                includes.append(norm_path(_join(action.cwd, value), repo_root, "", path_map))
            elif low[:3] in ("/fo", "-fo"):
                output = _repo_rel(_join(action.cwd, args[i + 1] if spaced else tok[3:]), repo_root)
            elif low not in ("/nologo", "-nologo"):
                options.append("/" + tok[1:] if tok.startswith("-") else tok)
            i += 2 if spaced else 1
        scripts.append(script)
        out.append({"script": _repo_rel(script, repo_root), "defines": sorted(set(defines)),
                    "includes": _one_spelling(includes), "options": options, "output": output})
    if out:
        reads, _, generated = _tracked_inputs(g, "rc", origins, repo_root, truecase, scripts)
        # The tracking log is per project, not per script: with several scripts
        # the files are what all of them read together.
        for entry in out:
            entry["reads"] = reads
            if generated:
                entry["generated_reads"] = generated
    return out


def _include_environment(g: _Group, origins: Origins, repo_root: str,
                         path_map: Sequence[Tuple[str, str]]) -> List[str]:
    """Entries of the INCLUDE variable the project's tools ran with that are
    not the toolchain's own directories, in order.

    The compiler and the resource compiler search INCLUDE after their `/I`
    directories, and nothing on a command line shows it. A RELATIVE entry is
    taken from the tool's working directory, and need not exist to matter:
    `#include "../../../x.h"` resolves through `<project>\\<anything>` as if the
    including file were one level deeper than it is. PowerToys has such an
    entry (the name of a property, leaked into the variable) and sources that
    only compile because of it."""
    value = ""
    for task in g.tasks("SetEnv"):
        if task.scalar("Name").upper() == "INCLUDE":
            value = task.scalar("Value")
    out: List[str] = []
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        absolute = _join(g.dir, entry)
        c = origins.classify(absolute)
        if c.kind in (origins_mod.MSVC, origins_mod.WINSDK, origins_mod.VS, origins_mod.DOTNET):
            continue
        spelled = norm_path(absolute, repo_root, "", path_map)
        if spelled not in out:
            out.append(spelled)
    return out


_CL_OPTIMIZATION = re.compile(r"^/(O[12dgistxy]|Ob[0-3]|Oi-?|Oy-?|GL-?|Gy-?|Gw-?|GF-?)$")
_LINK_OPTIMIZATION = re.compile(r"^/(LTCG(:[A-Za-z]+)?|OPT:[A-Za-z,]+)$", re.IGNORECASE)


def _optimization(compiles: List[Action], link: Optional[Action]) -> dict:
    """The optimization options of the build, as given: {compile, link}.

    The digest leaves them out as the build type (a Debug and a Release build
    of one project are the same build to the differ). They are reported apart
    so that a Bazel build can be given the same ones: whole-program
    optimization (`/GL` with `/LTCG`) decides what ends up in a binary, and a
    link can come to depend on unused code being dropped."""
    def options(action: Optional[Action], pattern) -> List[str]:
        found: List[str] = []
        for tok in (action.arguments if action else []):
            spelled = "/" + tok[1:] if tok.startswith("-") else tok
            if pattern.match(spelled) and spelled not in found:
                found.append(spelled)
        return found

    out: dict = {}
    compile_options = options(compiles[0] if compiles else None, _CL_OPTIMIZATION)
    link_options = options(link, _LINK_OPTIMIZATION)
    if compile_options:
        out["compile"] = compile_options
    if link_options:
        out["link"] = link_options
    return out


def _pri(g: _Group, repo_root: str, fs: bool) -> dict:
    """The project's resource index (`.pri`), from the tasks that built it: its
    name and output, the configuration file, and what went in, by kind.

      layout     files that are resources by being in the project (images):
                 {path, name}, `name` being the path the index knows them by
      embed      files stored in the index itself (compiled XAML): {path, name}
      resources  string tables (.resw)
      pri_files  indexes of libraries and packages, merged into this one

    The tasks are given candidates and write the lists they settle on to files
    (`*.resfiles`); those files are read when they are on this machine."""
    generate = [t for t in g.tasks("WinAppSdkGenerateProjectPriFile")
                if t.scalar("OutputFileName").lower().endswith(".pri")]
    configure = list(g.tasks("WinAppSdkGeneratePriConfigurationFiles"))
    if not generate or not configure:
        return {}
    task, lists = generate[-1], configure[-1]

    def written(parameter: str) -> Optional[List[str]]:
        path = lists.scalar(parameter)
        if not fs or not path:
            return None
        try:
            with open(_join(g.dir, path), encoding="utf-8-sig", errors="replace") as f:
                return [line.strip() for line in f if line.strip()]
        except OSError:
            return None

    def named(parameter: str, list_parameter: str) -> List[dict]:
        by_name: Dict[str, dict] = {}
        for item in lists.inputs.get(parameter, []):
            name = item.get("TargetPath") or ntpath.basename(win_norm(item.spec))
            by_name.setdefault(name.lower(), {
                "path": _repo_rel(_join(g.dir, item.spec), repo_root), "name": name})
        chosen = written(list_parameter)
        if chosen is None:
            return list(by_name.values())
        return [by_name.get(n.lower(), {"path": "", "name": n}) for n in chosen]

    def plain(parameter: str, list_parameter: str) -> List[str]:
        chosen = written(list_parameter)
        if chosen is None:
            chosen = [i.spec for i in lists.inputs.get(parameter, [])]
        return [_repo_rel(_join(g.dir, p), repo_root) for p in chosen]

    return {
        "output": _repo_rel(_join(g.dir, task.scalar("OutputFileName")), repo_root),
        "index_name": task.scalar("ProjectPriIndexName"),
        "config": _repo_rel(_join(g.dir, task.scalar("PriConfigXmlPath")), repo_root),
        "makepri": _repo_rel(task.scalar("MakePriExeFullPath"), repo_root),
        "layout": named("LayoutFiles", "FilteredLayoutResfilesPath"),
        "embed": named("EmbedFiles", "EmbedFileResfilePath"),
        "resources": plain("PRIResourceFiles", "ResourcesResfilesPath"),
        "pri_files": plain("PriFiles", "PriResfilesPath"),
    }


def _is_xaml_precompile(action: Action) -> bool:
    """The compile of a WinUI project's intermediate assembly (target
    XamlPreCompile): it writes into `<IntDir>intermediatexaml\\`."""
    out = next((a for a in action.arguments if a[:5].lower() in ("/out:", "-out:")), "")
    return "\\intermediatexaml\\" in out.replace("/", "\\").lower()


# Parameters of CompileXaml that are lists of files.
_XAML_FILE_LISTS = ("XamlPages", "XamlApplications", "SdkXamlPages", "ClIncludeFiles",
                    "ReferenceAssemblies", "ReferenceAssemblyPaths", "LocalAssembly")
# Parameters that are a place on the machine or in the build tree, which a rule
# supplies itself (the compiler package, its output directory).
_XAML_PLACES = ("ProjectPath", "OutputPath", "SavedStateFile", "GenXbfPath", "WindowsSdkPath",
                "VCInstallDir", "VCInstallPath32", "VCInstallPath64", "FingerprintIgnorePaths")


def _xaml(g: _Group, repo_root: str, path_map: Sequence[Tuple[str, str]]) -> dict:
    """How the project's XAML was compiled, from the tasks that did it.

    WinUI (`CompileXaml`, twice): per pass the pages and application
    definitions, the scalar parameters, the references that are NOT a
    package's (the compiler is told which are), the intermediate assembly of
    pass 2, and what it wrote (generated code, compiled XAML).

    WPF (`MarkupCompilePass1` / `MarkupCompilePass2` / `ResourcesGenerator`):
    the markup, the generated code and compiled markup of each pass, and the
    resources file the compiled markup is embedded through."""
    def files(items) -> List[str]:
        # A task output assigned to several item types is logged once for each.
        return list(dict.fromkeys(_repo_rel(_join(g.dir, i.spec), repo_root) for i in items))

    winui = list(g.tasks("CompileXaml"))
    if winui:
        out: dict = {"framework": "winui", "project": _repo_rel(g.file, repo_root)}
        for task in winui:
            first = task.scalar("IsPass1").lower() == "true"
            entry: dict = {
                "settings": {name: task.scalar(name) for name in sorted(task.inputs)
                             if name not in _XAML_FILE_LISTS and name not in _XAML_PLACES},
                "output_path": _repo_rel(_join(g.dir, task.scalar("OutputPath")), repo_root),
            }
            for key, parameter in (("pages", "XamlPages"), ("applications", "XamlApplications")):
                listed = []
                for item in task.inputs.get(parameter, []):
                    page = {"path": _repo_rel(_join(g.dir, item.spec), repo_root),
                            "spec": item.spec}
                    if item.get("Link"):
                        page["link"] = item.get("Link")
                    listed.append(page)
                if listed:
                    entry[key] = listed
            references = task.inputs.get("ReferenceAssemblies", [])
            # By file name, in the order given: the compiler numbers the types
            # it finds in that order (the type table of XamlTypeInfo.g.cs).
            entry["reference_order"] = [ntpath.basename(win_norm(i.spec)) for i in references]
            entry["references_not_from_packages"] = [
                norm_path(_join(g.dir, i.spec), repo_root, "", path_map)
                for i in references if not i.get("NuGetPackageId")]
            if task.inputs.get("LocalAssembly"):
                entry["local_assembly"] = files(task.inputs["LocalAssembly"])[0]
            for key, parameter in (("code", "GeneratedCodeFiles"), ("xaml", "GeneratedXamlFiles"),
                                   ("xbf", "GeneratedXbfFiles")):
                if task.outputs.get(parameter):
                    entry.setdefault("outputs", {})[key] = files(task.outputs[parameter])
            out["pass1" if first else "pass2"] = entry
        return out

    wpf = list(g.tasks("MarkupCompilePass1"))
    if wpf:
        task = wpf[0]
        out = {"framework": "wpf", "project": _repo_rel(g.file, repo_root)}
        first: dict = {
            "settings": {name: task.scalar(name) for name in sorted(task.inputs)
                         if name not in ("References", "SourceCodeFiles", "PageMarkup",
                                         "ApplicationMarkup", "ContentFiles", "OutputPath",
                                         "KnownReferencePaths", "AssembliesGeneratedDuringBuild",
                                         "SplashScreen", "ExtraBuildControlFiles")},
            "output_path": _repo_rel(_join(g.dir, task.scalar("OutputPath")), repo_root),
            "pages": [{"path": _repo_rel(_join(g.dir, i.spec), repo_root), "spec": i.spec}
                      for i in task.inputs.get("PageMarkup", [])],
            "applications": [{"path": _repo_rel(_join(g.dir, i.spec), repo_root), "spec": i.spec}
                             for i in task.inputs.get("ApplicationMarkup", [])],
            # By item spec: the generated code records these paths as written.
            "content": [i.spec for i in task.inputs.get("ContentFiles", [])],
            # What pass 2 picks up from pass 1 (`<Assembly>_MarkupCompile.cache`, `.lref`).
            "state": [p for p in files(task.outputs.get("AllGeneratedFiles", []))
                      if p.lower().endswith((".cache", ".lref"))],
            "requires_pass2": task.outputs.get("RequirePass2ForMainAssembly", [None])[0] is not None
            and task.outputs["RequirePass2ForMainAssembly"][0].spec.lower() == "true",
            "outputs": {"code": files(task.outputs.get("GeneratedCodeFiles", [])),
                        "baml": files(task.outputs.get("GeneratedBamlFiles", []))},
        }
        out["pass1"] = first
        for second in g.tasks("MarkupCompilePass2"):
            out["pass2"] = {
                "settings": {name: second.scalar(name) for name in sorted(second.inputs)
                             if name not in ("References", "OutputPath", "KnownReferencePaths",
                                             "AssembliesGeneratedDuringBuild")},
                "outputs": {"baml": files(second.outputs.get("GeneratedBaml", []))}}
        for resources in g.tasks("ResourcesGenerator"):
            out["resources"] = {
                "output": files(resources.outputs.get("OutputResourcesFile", []))[0]
                if resources.outputs.get("OutputResourcesFile") else "",
                "files": [{"path": _repo_rel(_join(g.dir, i.spec), repo_root),
                           **({"logical_name": i.get("LogicalName")} if i.get("LogicalName") else {}),
                           **({"link": i.get("Link")} if i.get("Link") else {})}
                          for i in resources.inputs.get("ResourceFiles", [])]}
        return out
    return {}


def _project_file_above(directory: str, repo_root: str, truecase: _TrueCase) -> str:
    """The project file in `directory` or the nearest directory above it, for a
    project this build log never ran. '' if none or off this machine."""
    root = win_norm(repo_root).rstrip("\\").lower()
    current = directory
    while truecase.enabled and current.lower().startswith(root + "\\"):
        try:
            names = sorted(os.listdir(current))
        except OSError:
            names = []
        found = next((n for n in names if n.lower().endswith(_PROJECT_FILE_EXTS)), "")
        if found:
            return truecase.path(ntpath.join(current, found))
        current = ntpath.dirname(current)
    return ""


def _pch(compile_actions: List[Action]) -> dict:
    """Precompiled-header use, reported because the differ treats /Yc /Yu as
    mechanics: which header, created by which source, and who opts out."""
    header, created_by, without = "", "", []
    for action in compile_actions:
        mode = ""
        for tok in action.arguments:
            if tok.startswith(("/Yc", "-Yc")):
                mode, header = "create", tok[3:].strip('"') or header
            elif tok.startswith(("/Yu", "-Yu")):
                mode, header = mode or "use", tok[3:].strip('"') or header
        if mode == "create":
            created_by = action.inputs[0]
        elif not mode:
            without.append(action.inputs[0])
    if not header and not created_by:
        return {}
    return {"header": header, "created_by": created_by, "not_using": without}


def _solution_configuration(log: events.BuildLog, repo_root: str) -> List[dict]:
    """Which project builds as what under this solution configuration. MSBuild
    passes the mapping to every project as an XML global property."""
    for proj in log.projects.values():
        xml = proj.globals.get("CurrentSolutionConfigurationContents")
        if not xml:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        rows = []
        for node in root.iter("ProjectConfiguration"):
            rows.append({
                "project": _repo_rel(node.get("AbsolutePath", ""), repo_root),
                "configuration": (node.text or "").strip(),
                "built": node.get("BuildProjectInSolution", "").lower() == "true",
            })
        return sorted(rows, key=lambda r: r["project"].lower())
    return []


# MSBuild's copy-if-newer target (`CopyToOutputDirectory=PreserveNewest`), and
# the item list a target before it computes for it.
_COPY_IF_NEWER = "_CopyOutOfDateSourceItemsToOutputDirectory"
_COPY_IF_NEWER_ITEMS = "_SourceItemsToCopyToOutputDirectory"


def _copies_already_there(g: _Group, inst: events.Project) -> List[Tuple[str, str]]:
    """(source, destination) of what MSBuild's copy-if-newer target was to
    deploy and did not hand to its Copy task, because the file was there.

    The target declares inputs and outputs, so MSBuild runs its Copy for the
    out-of-date part of the list only. Projects that build into one directory
    (PowerToys: every WinUI app into `WinUI3Apps`) each deploy the same
    runtime files there. The first one copies them; every later one logs a
    Copy without them, in a clean build as well. The list itself is in the log:
    the target that computed it added its items one by one."""
    ran = [t for t in inst.targets if t.name == _COPY_IF_NEWER]
    skipped = any(s.name == _COPY_IF_NEWER and s.reason == "OutputsUpToDate" for s in inst.skipped)
    out_dir = g.prop("OutDir")
    if not (ran or skipped) or not out_dir:
        return []
    listed: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for target in inst.targets:
        for added, item_type, items in target.item_changes:
            if item_type.lower() != _COPY_IF_NEWER_ITEMS.lower():
                continue
            for item in items:
                key = (item.spec.lower(), item.get("TargetPath").lower())
                if not added:
                    for stale in [k for k in listed if k[0] == key[0]]:
                        del listed[stale]
                elif item.get("TargetPath"):
                    listed.setdefault(key, (item.spec, item.get("TargetPath")))
    copied = {win_norm(_join(g.dir, i.spec)).lower()
              for t in ran for task in t.tasks if task.name.lower() == "copy"
              for i in task.inputs.get("DestinationFiles", [])}
    pairs = []
    for source, target_path in listed.values():
        destination = ntpath.join(out_dir, target_path)
        if win_norm(_join(g.dir, destination)).lower() not in copied:
            pairs.append((source, destination))
    return pairs


def _steps(g: _Group, repo_root: str) -> List[dict]:
    """Non-compile work the project did, for a human or an LLM to turn into
    Bazel rules: commands run, files written, files copied, and targets that
    declare inputs and outputs (shown even when skipped as up to date)."""
    rel = lambda p: _repo_rel(_join(g.dir, p), repo_root)
    steps: List[dict] = []
    seen: Set[str] = set()
    for inst in g.instances:
        for target in inst.targets:
            for task in target.tasks:
                low = task.name.lower()
                if low == "exec":
                    command = task.scalar("Command") or " ".join(task.command_lines)
                    key = "exec:" + command
                    if command and key not in seen:
                        seen.add(key)
                        steps.append({"kind": "exec", "target": target.name, "command": command,
                                      "cwd": rel(task.scalar("WorkingDirectory") or ".")})
                elif low == "copy":
                    sources = [i.spec for i in task.inputs.get("SourceFiles", [])]
                    dests = [i.spec for i in task.inputs.get("DestinationFiles", [])]
                    folder = task.scalar("DestinationFolder")
                    if sources:
                        step = {"kind": "copy", "target": target.name,
                                "from": [rel(s) for s in sources]}
                        step["to"] = [rel(d) for d in dests] if dests else rel(folder or ".")
                        key = "copy:%s:%s" % (step["from"], step["to"])
                        if key not in seen:
                            seen.add(key)
                            steps.append(step)
                elif low == "writelinestofile":
                    path = task.scalar("File")
                    # Tracker bookkeeping and file lists are not build inputs.
                    if not path or ".tlog" in path.lower() or path.lower().endswith(
                            (".filelistabsolute.txt", ".lastbuildstate", ".cache")):
                        continue
                    key = "write:" + path
                    if key in seen:
                        continue
                    seen.add(key)
                    step = {"kind": "write", "target": target.name, "file": rel(path)}
                    if path.lower().endswith(".rsp"):
                        # A response file IS the command line of the tool that
                        # reads it (cppwinrt, midl, mdmerge).
                        step["lines"] = [i.spec for i in task.inputs.get("Lines", [])]
                    steps.append(step)
        there = _copies_already_there(g, inst)
        key = "copy-there:%s" % sorted(d.lower() for _, d in there)
        if there and key not in seen:
            seen.add(key)
            steps.append({"kind": "copy", "target": _COPY_IF_NEWER, "up_to_date": True,
                          "from": [rel(s) for s, _ in there], "to": [rel(d) for _, d in there]})
        for skipped in inst.skipped:
            if skipped.reason == "OutputsUpToDate" and (skipped.inputs or skipped.outputs):
                key = "skipped:" + skipped.name
                if key in seen:
                    continue
                seen.add(key)
                steps.append({"kind": "target", "target": skipped.name, "ran": False,
                              "inputs": [rel(i) for i in skipped.inputs],
                              "outputs": [rel(o) for o in skipped.outputs]})
    return steps


def _exec_tool(command: str) -> str:
    """The program an `Exec` command runs. Exec commands go through cmd.exe,
    where a backslash does NOT escape a quote: the C++/WinRT targets write
    `"C:\\pkg\\bin\\"cppwinrt @x.rsp`, meaning `C:\\pkg\\bin\\cppwinrt`."""
    s = command.lstrip()
    if not s.startswith('"'):
        return wincmdline.split_tool(s)[0]
    end = s.find('"', 1)
    if end < 0:
        return ""
    rest = s[end + 1:]
    glued = rest.split(None, 1)[0] if rest and not rest[0].isspace() else ""
    return s[1:end] + glued


def _root_file(log: events.BuildLog) -> str:
    roots = [p for p in log.projects.values() if p.parent < 0]
    return roots[0].file if roots else ""


# ---- extraction -----------------------------------------------------------------

def extract(events_path: str, repo_root: str, *, use_tlogs: bool = True,
            fs: bool = True, design_time: Optional[str] = None
            ) -> Tuple[CanonicalModel, dict]:
    """Build the model and the report from an event dump.

    `design_time` is the event dump of a design-time pass over the managed
    projects (`msbuild_events.design_time_compile`): the source of C# command
    lines for compiles the build skipped.

    `fs=False` forbids touching the filesystem beyond the dump itself (no
    tracking logs, no git, no package hashes): what tests use, and what an
    extraction on a machine without the build tree degrades to.
    """
    repo_root = win_norm(repo_root) if is_abs(repo_root) else os.path.abspath(repo_root)
    log = events.load(events_path)
    groups = _groups(log, repo_root)
    design_time_log = events.load(design_time) if design_time else None
    computed_csc = _design_time_commands(design_time_log) if design_time_log else None
    computed_resources = _design_time_resources(design_time_log) if design_time_log else {}
    design_time_checks: Dict[str, dict] = {}
    use_tlogs = use_tlogs and fs
    truecase = _TrueCase(fs)

    origins = Origins(repo_root)
    for ev in log.evaluations.values():
        if len(ev.props):
            origins.learn(ev.props)
    if fs:
        origins.load_tracked_from_git()

    model = CanonicalModel(build_system=BuildSystem.MSBUILD, repo_root=repo_root)
    report_targets: Dict[str, dict] = {}
    incomplete: Dict[str, List[str]] = {}
    toolchains: Counter = Counter()

    # Pass 1: actions. Link outputs are needed before deps can be resolved.
    built: List[Tuple[_Group, Target, _Extracted, Optional[Action], str]] = []
    producers: Dict[str, str] = {}
    for g in groups:
        kind = _kind(g)
        target = Target(name=g.name, kind=kind)
        if g.ext == ".vcxproj":
            compiled = _compile_cpp(g, use_tlogs, truecase)
            target.actions.extend(compiled.actions)
            link, link_from = _link(g, kind, use_tlogs, truecase)
            if link is not None:
                target.actions.append(link)
            target.actions.extend(_tool_actions(g, "RC", "rc", "ResourceCompile",
                                                use_tlogs, truecase))
            target.actions.extend(_tool_actions(g, "MIDL", "midl", "Midl",
                                                use_tlogs, truecase))
            toolchains[(g.prop("VCToolsVersion"), g.prop("PlatformToolset"),
                        g.prop("WindowsTargetPlatformVersion") or g.prop("TargetPlatformVersion"),
                        _ARCH.get(g.prop("Platform").lower(), g.prop("Platform").lower()))] += 1
        else:
            compiled = _compile_csharp(g, computed_csc) if g.ext in MANAGED_EXTS \
                else _Extracted()
            if compiled.design_time_check is not None:
                design_time_checks[g.name] = compiled.design_time_check
            target.actions.extend(compiled.actions)
            link, link_from = None, ""
        for path in ([g.prop("TargetPath")] + (list(link.outputs) if link else [])):
            if path:
                producers[_join(g.dir, path).lower()] = g.name
        if link is not None:
            for tok in link.arguments:
                if tok.upper().startswith(("/IMPLIB:", "-IMPLIB:")):
                    producers[_join(g.dir, tok.split(":", 1)[1]).lower()] = g.name
        built.append((g, target, compiled, link, link_from))

    # Pass 2: roles, deps, annotations, report.
    for g, target, compiled, link, link_from in built:
        compiles = [a for a in target.actions if a.mnemonic == "CppCompile"]
        framework, evidence = _test_evidence(g, compiles)
        has_compile = any(a.mnemonic in ("CppCompile", "CSharpCompile") for a in target.actions)
        if g.ext in _PACKAGE_EXTS:
            target.role = TargetRole.PACKAGE
        elif framework:
            target.role = TargetRole.TEST
        elif not has_compile and not compiled.unrecovered:
            target.role = TargetRole.AGGREGATE
        else:
            target.role = TargetRole.PRODUCTION

        offered: List[dict] = []
        if link is not None:
            target.deps, offered = _resolve_libraries(g, link, producers, origins, fs, truecase)

        for action in compiles:
            for directory in cl_include_dirs(action.arguments):
                origins.note(_join(action.cwd, directory), "include", g.name)
            # A source a package has its consumers compile (an initializer the
            # package's targets file adds to the project).
            source = _join(action.cwd, action.inputs[0]) if action.inputs else ""
            if source and origins.classify(source).kind == origins_mod.NUGET:
                origins.note(source, "source", g.name)
        # Tools: the executable of every recorded action, and of every command
        # a target ran (cppwinrt, vcpkg, scripts via powershell).
        for action in target.actions:
            if action.arguments and is_abs(action.arguments[0]):
                origins.note(action.arguments[0], "tool", g.name)
        for task in g.tasks("Exec"):
            for command in task.command_lines or [task.scalar("Command")]:
                tool = _exec_tool(command or "")
                if tool and is_abs(tool):
                    origins.note(tool, "tool", g.name)
        for imported in g.evaluation.imports:
            if imported and is_abs(imported):
                c = origins.classify(imported)
                if c.kind in (origins_mod.NUGET, origins_mod.VCPKG):
                    origins.note(imported, "import", g.name)

        cfg, plat, tfm = _identity(g.evaluation)
        target.attrs = {k: v for k, v in {
            "project": _repo_rel(g.file, repo_root),
            "configuration": cfg, "platform": plat, "framework": tfm,
            "language": {".vcxproj": "c++", ".csproj": "c#", ".vbproj": "vb",
                         ".fsproj": "f#"}.get(g.ext, ""),
            "output": _repo_rel(_join(g.dir, g.prop("TargetPath")), repo_root)
            if g.prop("TargetPath") else "",
            "test_framework": framework,
        }.items() if v}
        model.add(target)
        if compiled.unrecovered:
            incomplete[g.name] = [_repo_rel(p, repo_root) for p in compiled.unrecovered]

        entry: dict = {
            "project": target.attrs.get("project", ""),
            "kind": target.kind.value, "role": target.role.value,
            "configuration": "%s|%s" % (cfg, plat) + (" " + tfm if tfm else ""),
            "output": target.attrs.get("output", ""),
            "built_in_this_log": g.built(),
            "recovered_from": dict(compiled.provenance),
        }
        if link is not None:
            entry["link_recovered_from"] = link_from
        if framework:
            entry["test"] = {"framework": framework, "evidence": evidence}
        if compiled.unrecovered:
            entry["unrecovered_sources"] = incomplete[g.name]
        refs = []
        for item in g.evaluation.items.get("ProjectReference", []):
            ref = {"project": _repo_rel(_join(g.dir, item.spec), repo_root)}
            for meta in ("LinkLibraryDependencies", "ReferenceOutputAssembly", "Private"):
                value = item.get(meta)
                if value and value.lower() == "false":
                    ref[meta] = "false"
            refs.append(ref)
        if refs:
            entry["project_references"] = refs
        if target.deps:
            entry["deps"] = [{k: v for k, v in (("name", d.name), ("external", d.external),
                                                ("origin", d.origin), ("version", d.version))
                              if v not in ("", None)} for d in target.deps]
        if offered:
            entry["libraries_offered_by_wildcard"] = offered
        packages = [{"id": i.spec, "version": i.get("Version")}
                    for i in g.evaluation.items.get("PackageReference", [])]
        if packages:
            entry["package_references"] = packages
        steps = _steps(g, repo_root)
        if steps:
            entry["steps"] = steps
        winrt = _winrt_metadata(target, steps, repo_root)
        if winrt:
            entry["winrt"] = winrt
        report_targets[g.name] = entry

    # The digests need the final path map, which is only complete once every
    # project's paths have been classified.
    model.path_map = origins.path_map()
    group_of = {g.name: g for g, *_ in built}
    # assembly file name -> the managed projects that write it (one per target
    # framework when a project multi-targets).
    managed_outputs: Dict[str, List[_Group]] = {}
    for g, *_ in built:
        if g.ext in MANAGED_EXTS:
            file_name = g.prop("TargetFileName") or ntpath.basename(g.prop("TargetPath"))
            if file_name:
                managed_outputs.setdefault(file_name.lower(), []).append(g)
    undeclared: Dict[str, List[str]] = {}
    for name, entry in report_targets.items():
        compiles = [a for a in model.targets[name].actions if a.mnemonic == "CppCompile"]
        if compiles:
            entry["compile"] = _digest(compiles, repo_root, model.path_map)
            pch = _pch(compiles)
            if pch:
                pch["created_by"] = _repo_rel(pch["created_by"], repo_root) if pch["created_by"] else ""
                pch["not_using"] = [_repo_rel(p, repo_root) for p in pch["not_using"]]
                entry["precompiled_header"] = pch
            generated = sorted({_repo_rel(a.inputs[0], repo_root) for a in compiles
                                if origins.classify(a.inputs[0]).kind == origins_mod.GENERATED})
            if generated:
                entry["generated_sources"] = generated
            external = _external_headers(group_of[name], compiles, repo_root, model.path_map)
            if external:
                entry["external_headers"] = external
            environment = _include_environment(group_of[name], origins, repo_root, model.path_map)
            if environment:
                entry["include_environment"] = environment
            optimization = _optimization(compiles, next(
                (a for a in model.targets[name].actions if a.mnemonic == "CppLink"), None))
            if optimization:
                entry["optimization"] = optimization
            if fs:
                dead = _dead_include_dirs(compiles, repo_root, model.path_map)
                if dead:
                    entry["dead_include_dirs"] = dead
            if use_tlogs:
                inputs = _generated_inputs(group_of[name], groups, origins, repo_root,
                                           truecase, compiles)
                if inputs:
                    entry["generated_inputs"] = inputs
                    loose = [i for i in inputs if i["produced_by"] == "another project"
                             and not i["referenced"]]
                    if loose:
                        undeclared[name] = [i["dir"] for i in loose]
                headers, external_reads, _ = _tracked_inputs(
                    group_of[name], "CL", origins, repo_root, truecase,
                    [a.inputs[0] for a in compiles if a.inputs])
                if headers:
                    entry["headers"] = headers
                if external_reads:
                    entry["external_reads"] = external_reads
        scripts = [a for a in model.targets[name].actions if a.mnemonic == "ResourceCompile"]
        if scripts:
            entry["resources"] = _resource_scripts(group_of[name], scripts, origins, repo_root,
                                                   truecase, model.path_map)
        link = next((a for a in model.targets[name].actions
                     if a.mnemonic in ("CppLink", "CppArchive")), None)
        if link is not None and link.mnemonic == "CppLink":
            entry["link_flags"] = list(canonicalize_link_flags(
                link.arguments, repo_root, cwd=link.cwd, path_map=model.path_map))
        other = Counter(a.mnemonic for a in model.targets[name].actions
                        if a.mnemonic in ("ResourceCompile", "Midl"))
        if other:
            entry["other_actions"] = dict(other)
        pri = _pri(group_of[name], repo_root, fs)
        if pri:
            entry["pri"] = pri
        compiles_cs = [a for a in model.targets[name].actions
                       if a.mnemonic == "CSharpCompile" and a.arguments]
        # A WinUI project compiles twice: first into an intermediate assembly
        # the XAML compiler's second pass reads. The project's compile is the
        # other one.
        intermediate = [a for a in compiles_cs if _is_xaml_precompile(a)]
        managed = next((a for a in compiles_cs if a not in intermediate),
                       compiles_cs[0] if compiles_cs else None)
        if managed is not None:
            g = group_of[name]
            inst = _build_instance(g)
            resx = _resource_sources(g.dir, g.tasks("GenerateResource"))
            if not resx and inst is not None:
                resx = computed_resources.get(_instance_key(g.file, inst.globals), {})
            entry["csharp"] = csharp.digest(
                managed.arguments, repo_root, managed.cwd, fs=fs,
                is_generated=lambda p: origins.classify(p).kind == origins_mod.GENERATED,
                project_of=lambda p, me=name: _managed_owner(p, managed_outputs, me),
                resource_sources={k: _repo_rel(v, repo_root) for k, v in resx.items()})
            entry["csharp"]["target_framework"] = g.prop("TargetFramework")
            for key, prop in (("runtime_identifier", "RuntimeIdentifier"),
                              ("root_namespace", "RootNamespace")):
                if g.prop(prop):
                    entry["csharp"][key] = g.prop(prop)
            if g.prop("ProjectAssetsFile"):
                # What NuGet resolved for this project: the input of nuget_assets.py.
                entry["csharp"]["assets_file"] = _repo_rel(
                    _join(g.dir, g.prop("ProjectAssetsFile")), repo_root)
            for host in g.tasks("CreateAppHost"):
                # The executable that starts the program: a copy of a pack's
                # apphost.exe with the assembly's name written into it.
                entry["csharp"]["apphost"] = {
                    "template": _repo_rel(host.scalar("AppHostSourcePath"), repo_root),
                    "binary": host.scalar("AppBinaryName"),
                    "gui": host.scalar("WindowsGraphicalUserInterface").lower() == "true",
                }
            xaml = _xaml(g, repo_root, model.path_map)
            if xaml:
                if intermediate and intermediate[0] is not managed:
                    xaml["precompile"] = csharp.digest(
                        intermediate[0].arguments, repo_root, intermediate[0].cwd, fs=fs,
                        is_generated=lambda p: origins.classify(p).kind == origins_mod.GENERATED,
                        project_of=lambda p, me=name: _managed_owner(p, managed_outputs, me),
                        resource_sources={})
                entry["csharp"]["xaml"] = xaml

    root_file = _root_file(log)
    root = next((p for p in log.projects.values() if p.parent < 0), None)
    model.config = {k: v for k, v in {
        "entry": _repo_rel(root_file, repo_root) if root_file else "",
        "configuration": (root.globals.get("Configuration", "") if root else ""),
        "platform": (root.globals.get("Platform", "") if root else ""),
        "build_succeeded": "" if log.succeeded is None else str(log.succeeded).lower(),
    }.items() if v}
    if toolchains:
        (msvc, toolset, sdk, arch), _ = toolchains.most_common(1)[0]
        model.toolchain = {k: v for k, v in {
            "msvc_version": msvc, "platform_toolset": toolset,
            "winsdk_version": sdk, "target_arch": arch}.items() if v}
    # .NET: the SDK whose compiler ran and the framework reference packs.
    model.toolchain.update(csc_dialect.dotnet_facts(
        a.arguments for t in model.targets.values() for a in t.actions
        if a.mnemonic == "CSharpCompile" and a.arguments))

    report = {
        "schema": REPORT_SCHEMA,
        "build": dict(model.config, **{
            "global_properties": {k: v for k, v in (root.globals.items() if root else [])
                                  if k != "CurrentSolutionConfigurationContents"},
            "environment_variables_read": sorted(log.env_read),
            "errors": [{"code": e.code, "file": _repo_rel(e.file, repo_root) if is_abs(e.file)
                        else e.file, "project": _repo_rel(e.project, repo_root)
                        if is_abs(e.project) else e.project, "message": e.message}
                       for e in log.errors],
        }),
        "toolchain": dict(model.toolchain, **{
            "all": [{"msvc_version": m, "platform_toolset": t, "winsdk_version": s,
                     "target_arch": a, "projects": n}
                    for (m, t, s, a), n in toolchains.most_common()],
        }),
        "solution_configuration": _solution_configuration(log, repo_root),
        "externals": _externals(origins, fs),
        # Generated files a project reads from a project it does not reference:
        # dependencies that exist only as build order.
        "undeclared_generated_inputs": undeclared,
        "targets": report_targets,
        "completeness": {
            "complete": not incomplete,
            "unrecovered": incomplete,
            "possibly_stale": sorted(
                n for n, e in report_targets.items()
                if e["recovered_from"].get("tlog_unverified")
                or e.get("link_recovered_from") == "tlog_unverified"),
        },
        "dumper_errors": log.dumper_errors,
    }
    if computed_csc is not None:
        # The design-time pass is trusted as far as it reproduces the command
        # lines of the projects this build DID compile.
        differing = {n: d for n, d in design_time_checks.items() if d}
        used = sorted(n for n, e in report_targets.items()
                      if e["recovered_from"].get("design_time"))
        report["completeness"]["design_time"] = {
            "status": ("mismatch" if differing else
                       "verified" if design_time_checks else "unchecked"),
            "checked_against_real_compiles": len(design_time_checks),
            "mismatched": differing,
            "used_for": used,
        }
    return model, report


def _externals(origins: Origins, fs: bool) -> List[dict]:
    out: List[dict] = []
    for root in origins.roots():
        used = (root.include_dirs or root.sources or root.libs or root.tools or root.imports
                or root.references)
        if not used and root.kind not in (origins_mod.MSVC, origins_mod.WINSDK):
            continue
        entry: dict = {"kind": root.kind, "token": root.token, "root": root.path}
        if root.id:
            entry["id"] = root.id
        if root.version:
            entry["version"] = root.version
        for key, values in (("include_dirs", root.include_dirs), ("sources", root.sources),
                            ("libs", root.libs),
                            ("tools", root.tools), ("build_imports", root.imports),
                            ("references", root.references)):
            if values:
                entry[key] = _one_spelling(values)
        if root.consumers:
            entry["consumers"] = sorted(root.consumers)
        if fs and root.kind == origins_mod.NUGET:
            entry.update(origins.nuget_facts(root))
        if fs and root.kind == origins_mod.VCPKG and root.token.startswith("@vcpkg"):
            # The tree a build used is `<installed>\<triplet>`; vcpkg's database
            # sits beside it and also lists host-tool ports of other triplets.
            triplet = root.token[len("@vcpkg/"):]
            installed = ntpath.dirname(root.path) if triplet else root.path
            ports = [p for p in origins.vcpkg_ports(installed)
                     if not triplet or p["triplet"] == triplet]
            if ports:
                entry["ports"] = ports
        out.append(entry)
    return out


def _one_spelling(paths: Iterable[str]) -> List[str]:
    """Sorted, with paths that differ only in case merged. A tracking log
    upper-cases what a command line in the log spells properly; keep the
    spelling that is not all upper-case."""
    chosen: Dict[str, str] = {}
    for path in sorted(paths):
        key = path.lower()
        if key not in chosen or chosen[key] == chosen[key].upper():
            chosen[key] = path
    return sorted(chosen.values(), key=str.lower)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract a canonical action model from an MSBuild binary log.")
    parser.add_argument("source", help="msbuild.binlog, or an events.ndjson dumped earlier")
    parser.add_argument("repo_root")
    parser.add_argument("out", help="model JSON to write")
    parser.add_argument("--report", help="also write the migration report (JSON)")
    parser.add_argument("--msbuild", help="MSBuild.exe to replay the binlog with "
                        "(default: newest Visual Studio; must be at least as new as the "
                        "MSBuild that wrote the binlog)")
    parser.add_argument("--events", help="where to write the event dump when given a binlog "
                        "(default: <binlog>.events.ndjson)")
    parser.add_argument("--no-tlog", action="store_true",
                        help="do not recover skipped compiles from tracking logs")
    parser.add_argument("--design-time", action="store_true",
                        help="C#: also run a design-time pass (no compilation) to recover "
                             "the compiler command line of projects the build skipped as "
                             "up to date, and cross-check it against those it compiled")
    parser.add_argument("--design-time-events",
                        help="event dump of an earlier design-time pass to reuse")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit 0 even if some sources could not be recovered")
    args = parser.parse_args(argv)

    source = args.source
    if source.lower().endswith(".binlog"):
        dump = args.events or source + ".events.ndjson"
        used = events.dump_binlog(source, dump, args.msbuild)
        print(f"replayed {source} with {used} -> {dump}")
        source = dump

    design_time = args.design_time_events
    if args.design_time and not design_time:
        design_time = os.path.splitext(source)[0] + ".designtime.ndjson"
        instances = design_time_instances(events.load(source), win_norm(args.repo_root))
        if instances:
            events.design_time_compile(instances, design_time, args.msbuild)
            print(f"design-time pass over {len(instances)} managed project(s) -> {design_time}")
        else:
            design_time = None

    model, report = extract(source, args.repo_root, use_tlogs=not args.no_tlog,
                            design_time=design_time)
    dump_model(model, args.out)
    print(f"wrote {args.out}")
    if args.report:
        import json
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(f"wrote {args.report}")

    completeness = report["completeness"]
    recovered = Counter()
    for entry in report["targets"].values():
        recovered.update(entry["recovered_from"])
    print("targets: %d   compile actions: %s" % (len(model.targets), dict(recovered)))
    checked = completeness.get("design_time")
    if checked:
        print("design-time pass: %s (%d project(s) compared with their real compile, %d differ; "
              "used for %d)" % (checked["status"], checked["checked_against_real_compiles"],
                                len(checked["mismatched"]), len(checked["used_for"])))
        for name, fields in sorted(checked["mismatched"].items()):
            print("  %s differs in: %s" % (name, ", ".join(sorted(fields))))
    if completeness["possibly_stale"]:
        print("WARNING: %d project(s) have commands taken from tracking logs that this build "
              "did not re-check (it stopped before reaching them); they may be stale: %s"
              % (len(completeness["possibly_stale"]), ", ".join(completeness["possibly_stale"])))
    if not completeness["complete"]:
        total = sum(len(v) for v in completeness["unrecovered"].values())
        print("ERROR: %d source(s) in %d project(s) have no recovered compile command:"
              % (total, len(completeness["unrecovered"])), file=sys.stderr)
        for name, sources in sorted(completeness["unrecovered"].items()):
            print("  %s: %s%s" % (name, ", ".join(sources[:4]),
                                  " (+%d more)" % (len(sources) - 4) if len(sources) > 4 else ""),
                  file=sys.stderr)
        print("Build the project (a clean build is not required; an incremental build that "
              "succeeds is enough) and extract again, or pass --allow-incomplete.",
              file=sys.stderr)
        return 0 if args.allow_incomplete else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
