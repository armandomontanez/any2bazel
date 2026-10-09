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

"""Reconstruct comparable views from raw Actions -- the differ's smart layer.

The model is a faithful record of build Actions (raw argv + annotations). This
module interprets those actions into the shape the comparison operates on:
  * compile actions  -> TranslationUnits (source + canonicalized flags)
  * link actions     -> link_flags (canonicalized)
  * deps             -> resolved annotation if the frontend knew them (CMake),
                        otherwise INFERRED from link argv (Bazel only has argv)

All build-system-specific argv interpretation lives here, keyed on the model's
`build_system` tag (noise stripping) and each action's mnemonic (grouping). This
is the "more complexity in the differ" the action-based IR trades for a neutral
model. canonicalize.py (pure flag policy) is imported, not duplicated.

A TargetView is what diff.py compares -- the action model must re-derive exactly
the TU/link/dep results the old per-TU extractors produced (regression anchor).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import canonicalize_msvc as msvc
from canonicalize import canonicalize_flags, canonicalize_link_flags
from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TranslationUnit)

_COMPILE_MNEMONICS = {"CppCompile", "ObjcCompile"}
_LINK_MNEMONICS = {"CppLink", "CppArchive"}
_JAVA_COMPILE_MNEMONICS = {"JavaCompile"}
_CSHARP_COMPILE_MNEMONICS = {"CSharpCompile"}

_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx", ".inc", ".inl")
_SOURCE_EXTS = (".cc", ".cpp", ".cxx", ".c", ".C", ".m", ".mm")
_ARCHIVE_EXTS = (".a", ".lo", ".lib")
_JAVA_EXT = ".java"


@dataclass
class CompileGroup:
    """A whole-source-set compile unit (e.g. one javac invocation): N sources +
    shared flags, compared as a unit. This is the Java analog of a C++ TU -- C++
    compiles per-file (one TU each), Java compiles a source set at once, so its
    comparable unit is the (sources, flags) group, not a per-file TU.

    `key` identifies the group across builds (the output dir, made repo-relative)
    so the same logical compile aligns. Flags are NOT yet canonicalized -- that
    policy is deferred until a real Java-vs-Java diff shows what noise to strip.
    """
    key: str
    sources: Tuple[str, ...] = ()      # sorted, repo-relative
    flags: Tuple[str, ...] = ()        # the action's non-source argv (raw, for now)
    language: str = "java"             # "java" | "csharp": which source set it joins


@dataclass
class TargetView:
    """Reconstructed, comparison-ready view of a Target: TUs (C/C++) and/or
    CompileGroups (Java) + link flags + the (resolved or inferred) dep set.
    diff.py operates on these, not on raw actions."""
    name: str
    kind: TargetKind
    role: object                       # TargetRole (avoid import churn)
    tus: List[TranslationUnit] = field(default_factory=list)
    compile_groups: List[CompileGroup] = field(default_factory=list)
    link_flags: Tuple[str, ...] = ()
    deps: List[Dependency] = field(default_factory=list)
    language: str = ""                 # "c++" / "c#" when the target is single-language

    def tu_map(self):
        return {tu.key(): tu for tu in self.tus}


# ---- argv interpretation (was split across the two extractors) -------------

def _is_header_processing(args) -> bool:
    """Bazel header self-containment actions (parse_headers / layering_check):
    compile a HEADER standalone (`-xc++-header`, `.processed` output), not a TU.
    CMake has no equivalent; counting them would show every header as extra_tu."""
    return "-xc++-header" in args or "-fsyntax-only" in args


def _source_from_compile_args(args) -> Optional[str]:
    """The compiled source is the arg to -c (or the lone source arg). Returns
    None for header inputs -- not translation units."""
    src = None
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args):
            src = args[i + 1]
            break
    if src is None:
        for a in args:
            if a.endswith(_SOURCE_EXTS):
                src = a
                break
    if src is None or src.endswith(_HEADER_EXTS):
        return None
    return src


def _rel(path: str, repo_root: str) -> str:
    """Make a source path repo-relative, POSIX-normalized. Handles both an
    absolute path under repo_root (CMake) and an already-workspace-relative path
    (Bazel bazel-out/..., or a source path). Absolute paths outside repo_root and
    relative paths are returned normalized but unchanged."""
    if repo_root and os.path.isabs(path):
        try:
            r = os.path.relpath(path, repo_root)
            if not r.startswith(".."):
                return r.replace(os.sep, "/")
        except ValueError:
            pass
    p = path.replace(os.sep, "/")
    rr = repo_root.replace(os.sep, "/").rstrip("/")
    if rr and p.startswith(rr + "/"):
        p = p[len(rr) + 1:]
    return p


def _java_compile_group(args, repo_root: str) -> "CompileGroup":
    """Split a JavaCompile argv into (sources, flags) and key the group by its
    output dir (`-d`). Sources are the .java args, made repo-relative + sorted;
    flags are everything else (incl. -classpath/-sourcepath paths) kept RAW for
    now -- Java flag canonicalization is deferred until a real diff needs it."""
    sources, flags = [], []
    out_key = ""
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "-d" and i + 1 < n:
            out_key = _rel(args[i + 1], repo_root)
            flags.append(a); flags.append(args[i + 1]); i += 2; continue
        if a.endswith(_JAVA_EXT):
            sources.append(_rel(a, repo_root)); i += 1; continue
        flags.append(a); i += 1
    return CompileGroup(key=out_key or "<javac>",
                        sources=tuple(sorted(sources)), flags=tuple(flags))


_CSHARP_EXT = ".cs"


def _csharp_compile_group(act: Action, repo_root: str, path_map) -> "CompileGroup":
    """One csc invocation as a source set. Sources are the `.cs` arguments, or
    the declared inputs when the argv is not available (Bazel passes them in a
    param file; a compile MSBuild skipped as up to date has no argv at all).
    Keyed by the `/out:` assembly. Flags are kept raw, like Java's."""
    sources, flags = [], []
    out_key = ""
    for a in act.arguments:
        low = a.lower()
        if low.startswith(("/out:", "-out:")):
            out_key = msvc.norm_path(a[5:], repo_root, act.cwd, path_map)
            flags.append(a)
        elif low.endswith(_CSHARP_EXT) and not a.startswith(("/", "-", "@")):
            sources.append(msvc.norm_path(a, repo_root, act.cwd, path_map))
        else:
            flags.append(a)
    if not sources:
        sources = [msvc.norm_path(p, repo_root, act.cwd, path_map)
                   for p in act.inputs if p.lower().endswith(_CSHARP_EXT)]
    return CompileGroup(key=out_key or "<csc>", sources=tuple(sorted(sources)),
                        flags=tuple(flags), language="csharp")


_LINK_INPUT_LIB_EXTS = (".a", ".lo", ".lib", ".so", ".dylib")


def _lib_identity(arg) -> Optional[str]:
    """Abstract dep name for an archive/shared-lib FILE link input:
    'libcatch2_main.a' -> 'catch2_main', 'libfmt.so.12' -> 'fmt'. None if `arg`
    is a flag/object/output, not a library file. Handles versioned solibs
    (libfmt.so.12.2.0) by taking the stem before the first dot."""
    if arg.startswith("-"):
        return None
    base = os.path.basename(arg)
    is_lib = base.endswith(_LINK_INPUT_LIB_EXTS) or ".so." in base
    if not is_lib:
        return None
    stem = base.split(".")[0]
    if stem.startswith("lib"):
        stem = stem[3:]
    return stem or None


def _archive_identity(arg) -> Optional[str]:
    """Abstract dep name for an archive FILE link input: 'libcatch2_main.a' ->
    'catch2_main'. None if `arg` is a flag/object/output, not an archive.
    (Kept for the argv path, which historically only saw archives; shared-lib
    inputs come through _lib_identity on the link action's declared inputs.)"""
    if arg.startswith("-") or not arg.endswith(_ARCHIVE_EXTS):
        return None
    stem = os.path.basename(arg).split(".")[0]
    if stem.startswith("lib"):
        stem = stem[3:]
    return stem or None


def _is_external_path(arg) -> bool:
    """An external/third-party (bzlmod) archive vs an in-project library:
    Bazel puts external repos under 'external/<repo>'."""
    return "external/" in arg


def _infer_deps_from_link(actions, existing) -> List[Dependency]:
    """Infer link deps from a link action's argv AND its declared library inputs
    (Bazel only annotates argv + inputs, no resolved dep list like CMake).

      * argv `-l<name>`            -> external system lib
      * argv archive file token    -> external if under external/, else internal
      * declared INPUT lib file    -> archive/solib fed to the linker by path
                                       rather than -l (the common Bazel case);
                                       external if under external/, else internal

    Reading the declared inputs is what makes statically-linked archives and
    external solibs visible even when they never appear as a -l argv token.
    Dedup against names already present."""
    deps: List[Dependency] = list(existing)
    have = {d.name for d in deps}
    for act in actions:
        if act.mnemonic not in _LINK_MNEMONICS:
            continue
        if msvc.is_msvc_link(act.arguments):
            # Windows naming: no `lib` prefix to strip, dots are part of the
            # name, case does not matter. A library is linked when the command
            # line names it (positional, /DEFAULTLIB:, /WHOLEARCHIVE:). A
            # declared input is NOT evidence: a whole directory offered through
            # /LIBPATH (the Spectre CRT, a vcpkg lib dir) is an input, and
            # counting it would report every library in it as linked and hide a
            # missing one. Inputs are only used when the command line is
            # unknown because its response file was not expanded.
            libs = list(msvc.link_libraries(act.arguments))
            if any(a.startswith("@") for a in act.arguments[1:]):
                libs += [p for p in act.inputs if p.lower().endswith(".lib")]
            for lib in libs:
                ident = msvc.lib_identity(lib)
                has_dir = "/" in lib or "\\" in lib
                if ident and ident not in have:
                    # A bare name is resolved through LIB: a system library.
                    deps.append(Dependency(ident, external=(not has_dir) or _is_external_path(lib)))
                    have.add(ident)
            continue
        for a in act.arguments:
            if a.startswith("-l"):
                name = a[2:]
                if name and name not in have:
                    deps.append(Dependency(name, external=True)); have.add(name)
                continue
            ident = _archive_identity(a)
            if ident and ident not in have:
                deps.append(Dependency(ident, external=_is_external_path(a)))
                have.add(ident)
        for path in act.inputs:
            ident = _lib_identity(path)
            if ident and ident not in have:
                deps.append(Dependency(ident, external=_is_external_path(path)))
                have.add(ident)
    return deps


# ---- the reconstruct entry points ------------------------------------------

def _is_msvc(act: Action, build_system: BuildSystem) -> bool:
    """Is this action in the MSVC dialect? Decided per ACTION, not per build
    system: a Bazel build is MSVC-dialect when its toolchain is cl.exe or
    clang-cl, and an MSBuild C++ action always is (its argv may have come from
    a tracking log, which stores switches without the tool)."""
    if build_system == BuildSystem.MSBUILD:
        return True
    return msvc.is_msvc_compile(act.arguments) or msvc.is_msvc_link(act.arguments)


def reconstruct_target(t: Target, build_system: BuildSystem,
                       repo_root: str, path_map=()) -> TargetView:
    is_bazel = build_system == BuildSystem.BAZEL
    view = TargetView(name=t.name, kind=t.kind, role=t.role,
                      language=t.attrs.get("language", ""))

    for act in t.actions:
        if act.mnemonic in _COMPILE_MNEMONICS:
            if _is_msvc(act, build_system):
                src = msvc.cl_source(act.arguments)
                if not src:
                    continue
                cdef, cinc, cfl = msvc.canonicalize_cl_flags(
                    act.arguments, repo_root, cwd=act.cwd, path_map=path_map)
                view.tus.append(TranslationUnit(
                    source=msvc.norm_path(src, repo_root, act.cwd, path_map),
                    defines=cdef, includes=cinc, flags=cfl))
                continue
            if _is_header_processing(act.arguments):
                continue
            src = _source_from_compile_args(act.arguments)
            if not src:
                continue
            cdef, cinc, cfl = canonicalize_flags(
                list(act.arguments), repo_root, is_bazel=is_bazel)
            view.tus.append(TranslationUnit(
                source=_rel(src, repo_root), defines=cdef,
                includes=cinc, flags=cfl))
        elif act.mnemonic in _LINK_MNEMONICS:
            # CppArchive (static lib) has no meaningful link flags; only CppLink.
            if act.mnemonic == "CppLink":
                if _is_msvc(act, build_system):
                    view.link_flags = msvc.canonicalize_link_flags(
                        act.arguments, repo_root, cwd=act.cwd, path_map=path_map)
                else:
                    view.link_flags = canonicalize_link_flags(
                        list(act.arguments), is_bazel=is_bazel)
        elif act.mnemonic in _JAVA_COMPILE_MNEMONICS:
            # Java compiles a whole source set per action -> one CompileGroup.
            view.compile_groups.append(
                _java_compile_group(act.arguments, repo_root))
        elif act.mnemonic in _CSHARP_COMPILE_MNEMONICS:
            # So does C#: one csc invocation per assembly.
            view.compile_groups.append(
                _csharp_compile_group(act, repo_root, path_map))
    if not view.language and view.compile_groups and not view.tus \
            and all(g.language == "csharp" for g in view.compile_groups):
        view.language = "c#"

    # deps: trust the frontend's resolved annotation (CMake); else infer from
    # link argv (Bazel).
    if t.deps:
        view.deps = list(t.deps)
    else:
        view.deps = _infer_deps_from_link(t.actions, t.deps)
    return view


def reconstruct(model: CanonicalModel) -> dict:
    """Reconstruct every target into a TargetView, keyed by target name."""
    return {name: reconstruct_target(t, model.build_system, model.repo_root,
                                     model.path_map)
            for name, t in model.targets.items()}
