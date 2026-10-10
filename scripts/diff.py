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

"""Asymmetric parity diff: CMake reference model (A) vs Bazel model (B).

Produces a worklist of discrepancies for the fix loop. Each iteration of the
migrate loop calls this; an empty worklist (for a full round) means "done".
Parity has two stages, both reported here:
  COMPILE PARITY    -- per-TU flag/define/include equivalence (project-wide
                       TU-set) + external link-dependency closure.
  LINK CONSISTENCY  -- per name-aligned executable/shared-lib, equivalent link
                       flags (link_flags_diff).

ASYMMETRY: a flag present in CMake but missing in Bazel is a discrepancy
(under-specified Bazel target -> likely wrong build). A flag present only in
Bazel is tolerated (toolchain defaults already stripped in canonicalize). The
one exception is defines, where an EXTRA Bazel define can change behavior, so
extra defines are reported at lower severity.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional

import canonicalize_csc as csc_dialect
import canonicalize_msvc as msvc_dialect
from canonicalize import BAZEL_TOLERATED_FLAG_PREFIXES
from config import MigrationConfig
from model import (BuildSystem, CanonicalModel, TargetKind, TargetRole,
                   TranslationUnit)
from reconstruct import TargetView, reconstruct


def _display_bazel_only(flags) -> list:
    """Cosmetic filter for the `bazel_only` side of a flag diff. Bazel-injected
    tolerated defaults (-fstack-protector, -fdiagnostics-color, ...) are kept
    through canonicalization so they CANCEL against a project flag both sides
    set (see canonicalize.BAZEL_TOLERATED_FLAG_PREFIXES). When only Bazel has
    them they're harmless toolchain noise, so drop them from the reported
    bazel_only list to keep the worklist readable. Never affects severity --
    bazel_only is always tolerated; this only tidies the display."""
    return sorted(f for f in flags
                  if not any(f == p or f.startswith(p)
                             for p in BAZEL_TOLERATED_FLAG_PREFIXES))


class Severity(str, Enum):
    ERROR = "error"      # blocks parity: must fix to converge
    WARN = "warn"        # likely benign (e.g. extra bazel define) but surfaced


class Kind(str, Enum):
    MISSING_TARGET = "missing_target"
    EXTRA_TARGET = "extra_target"
    KIND_MISMATCH = "kind_mismatch"
    MISSING_TU = "missing_tu"
    EXTRA_TU = "extra_tu"
    DEFINES_DIFF = "defines_diff"
    INCLUDES_DIFF = "includes_diff"
    FLAGS_DIFF = "flags_diff"
    LINK_FLAGS_DIFF = "link_flags_diff"   # executable/shared-lib link flags
    MISSING_DEP = "missing_dep"
    # Java (CompileGroup) source-set comparison:
    MISSING_JAVA_SRC = "missing_java_src"  # .java compiled in A but not B
    EXTRA_JAVA_SRC = "extra_java_src"      # .java compiled in B but not A
    # C# (CompileGroup) source-set comparison (only when cfg.include_csharp):
    MISSING_CS_SRC = "missing_cs_src"      # .cs compiled in A but not B
    EXTRA_CS_SRC = "extra_cs_src"          # .cs compiled in B but not A
    CS_REFS_DIFF = "cs_refs_diff"          # referenced assemblies (and their packages)
    CS_ANALYZERS_DIFF = "cs_analyzers_diff"  # analyzers / source generators
    CS_RESOURCES_DIFF = "cs_resources_diff"  # resources, additional files, analyzer configs
    # A source the reference compiles with more distinct flag sets than Bazel:
    TU_VARIANT_COUNT = "tu_variant_count"
    # The two builds resolved different toolchains (compiler / SDK / arch):
    TOOLCHAIN_DIFF = "toolchain_diff"
    # test-specific (only emitted when cfg.include_tests):
    MISSING_TEST_TU = "missing_test_tu"     # test source compiled in cmake, not bazel
    EXTRA_TEST_TU = "extra_test_tu"         # test source compiled in bazel, not cmake
    TEST_BINARY_COUNT = "test_binary_count"  # differing number of test executables


@dataclass
class Discrepancy:
    kind: str
    severity: str
    target: str
    detail: str
    tu: Optional[str] = None
    cmake_only: Optional[list] = None  # present in A, absent in B  -> must add
    bazel_only: Optional[list] = None  # present in B, absent in A  -> usually ok


def _diff_tu(target: str, a: TranslationUnit, b: TranslationUnit,
             cfg: "MigrationConfig", fold: bool = False) -> List[Discrepancy]:
    """`fold` compares include paths case-insensitively (Windows builds)."""
    out: List[Discrepancy] = []

    # defines: order-insensitive set compare; both directions matter.
    # Reviewer-approved ignores (cfg) are dropped from BOTH sides first.
    a_def = {d for d in a.defines if not cfg.define_ignored(d)}
    b_def = {d for d in b.defines if not cfg.define_ignored(d)}
    if a_def - b_def or b_def - a_def:
        out.append(Discrepancy(
            kind=Kind.DEFINES_DIFF.value,
            severity=Severity.ERROR.value if (a_def - b_def) else Severity.WARN.value,
            target=target, tu=a.source,
            detail="defines differ",
            cmake_only=sorted(a_def - b_def),
            bazel_only=sorted(b_def - a_def),
        ))

    # includes: PRESENCE check (order currently NOT enforced -- see below).
    # Two normalizations, applied to both sides first:
    #   1. include_map: rewrite differing spellings of the same dep root to a
    #      canonical token, so the check is PRESERVED (the dep must still be
    #      present). Collapse adjacent dups the rewrite produces (Bazel's
    #      external/X and bazel-out/.../external/X both -> the token).
    #   2. include_ignored: drop blind-spot prefixes entirely.
    #
    # We require every CMake include root to be PRESENT on the Bazel side, but
    # do NOT (yet) enforce relative ORDER. Order only changes the build when the
    # same header name is reachable from two roots whose order differs; proving
    # that requires enumerating headers on disk (a collision check). Until that
    # verifier exists, enforcing order produced benign false positives (e.g.
    # boringssl: project `include` vs vendored gtest roots, disjoint headers,
    # reordered). See docs/FUTURE-include-order-collision-check.md.
    a_inc = _norm_includes(a.includes, cfg, fold)
    b_inc = _norm_includes(b.includes, cfg, fold)
    same = (lambda s: s.lower()) if fold else (lambda s: s)
    a_keys, b_keys = {same(i) for i in a_inc}, {same(i) for i in b_inc}
    missing = [i for i in a_inc if same(i) not in b_keys]
    if missing:
        out.append(Discrepancy(
            kind=Kind.INCLUDES_DIFF.value,
            severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="cmake include root missing on bazel side",
            cmake_only=missing,
            bazel_only=[i for i in b_inc if same(i) not in a_keys],
        ))

    # other flags: asymmetric subset -- A must be subset of B.
    # Reviewer-approved ignores are dropped from both sides first.
    a_fl = {f for f in a.flags if not cfg.flag_ignored(f)}
    b_fl = {f for f in b.flags if not cfg.flag_ignored(f)}
    stray = _changed_defaults(a_fl, b_fl, msvc_dialect.default_family)
    if a_fl - b_fl:
        out.append(Discrepancy(
            kind=Kind.FLAGS_DIFF.value,
            severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="cmake compile flags missing on bazel side",
            cmake_only=sorted(a_fl - b_fl),
            bazel_only=_display_bazel_only(b_fl - a_fl),
        ))
    elif stray:
        out.append(Discrepancy(
            kind=Kind.FLAGS_DIFF.value, severity=Severity.ERROR.value,
            target=target, tu=a.source,
            detail="bazel changes a compiler default the reference keeps",
            cmake_only=[], bazel_only=stray))
    return out


def _changed_defaults(a_flags, b_flags, family_of) -> List[str]:
    """Bazel-side flags that move a setting off the compiler's default while
    the reference says nothing about that setting (so it has the default).
    Extra flags are normally tolerated; these are not extra, they are a
    different value."""
    a_families = {family_of(f) for f in a_flags} - {None}
    return sorted(f for f in b_flags - a_flags
                  if family_of(f) and family_of(f) not in a_families)


def _diff_link_flags(name: str, ta: "TargetView", tb: "TargetView",
                     cfg: "MigrationConfig") -> List[Discrepancy]:
    """Asymmetric link-flag subset for one aligned executable/shared-lib: every
    CMake link flag must be present on the Bazel side; extra Bazel link flags
    are tolerated. Reviewer-approved differences (cfg.link_flag_ignored) are
    dropped from both sides first. Mirrors the compile-flag check."""
    a_lf = {f for f in ta.link_flags if not cfg.link_flag_ignored(f)}
    b_lf = {f for f in tb.link_flags if not cfg.link_flag_ignored(f)}
    if a_lf - b_lf:
        return [Discrepancy(
            kind=Kind.LINK_FLAGS_DIFF.value,
            severity=Severity.ERROR.value,
            target=name,
            detail="cmake link flags missing on bazel side",
            cmake_only=sorted(a_lf - b_lf),
            bazel_only=sorted(b_lf - a_lf),
        )]
    return []


def _norm_includes(includes, cfg, fold: bool = False) -> tuple:
    """Apply include_map rewrites then drop ignored prefixes, collapsing the
    consecutive duplicates a rewrite can create (e.g. external/X and its
    bazel-out twin both map to the same token). Order preserved. `fold`
    matches the config's prefixes case-insensitively (Windows builds)."""
    out = []
    for inc in includes:
        mapped = cfg.map_include(inc, fold)
        if cfg.include_ignored(mapped, fold) or cfg.include_ignored(inc, fold):
            continue
        if not out or out[-1] != mapped:
            out.append(mapped)
    return tuple(out)


# Roles the parity diff actually compares. Everything else (dashboard,
# aggregate, codegen, test, unknown) is retained in the model and reported
# separately for inspection, but never produces a discrepancy. Flip TEST on
# here when the test-migration phase begins -- no re-extraction needed.
PARTICIPATING_ROLES = {TargetRole.PRODUCTION}


def _out_of_scope_language(t: TargetView, cfg: "MigrationConfig") -> bool:
    """C# targets take part only when opted in (cfg.include_csharp)."""
    return t.language == "c#" and not cfg.include_csharp


def _participating(views: Dict[str, TargetView], cfg: "MigrationConfig") -> set:
    return {n for n, t in views.items()
            if t.role in PARTICIPATING_ROLES and not cfg.target_excluded(n)
            and not _out_of_scope_language(t, cfg)}


def _apply_target_map(views: Dict[str, TargetView],
                      target_map: Dict[str, str]) -> Dict[str, TargetView]:
    """Rename Bazel-side target VIEWS into the CMake namespace using a declared
    map (cmake_name -> bazel_name), so intentionally-renamed targets align.

    The map is authored by whoever generates the BUILD files -- an explicit
    input, not a fuzzy guess. Returns a re-keyed dict; deps (which reference
    target names) are renamed too so link-closure comparison lines up.
    """
    if not target_map:
        return views
    from dataclasses import replace
    b2cmake = {bazel: cmake for cmake, bazel in target_map.items()}
    renamed: Dict[str, TargetView] = {}
    for name, t in views.items():
        t.name = b2cmake.get(name, name)
        t.deps = [replace(d, name=b2cmake.get(d.name, d.name)) for d in t.deps]
        renamed[t.name] = t
    return renamed


_LIBRARY_KINDS = {TargetKind.STATIC, TargetKind.SHARED, TargetKind.OBJECT,
                  TargetKind.INTERFACE}


def _union_tus(views: Dict[str, TargetView], names,
               cfg: "MigrationConfig") -> Dict[str, TranslationUnit]:
    """Pool TUs of the given target views into one source-keyed map (the TU-SET
    comparison): grouping/renames/fold-ins don't matter -- every compiled source
    lands in one flat map keyed by repo-relative path. Keys are run through
    cfg.map_source so generated-source grouping asymmetries (e.g. CMake's single
    AUTOMOC bundle vs Bazel's per-header moc_*.cpp) collapse to one token."""
    out: Dict[str, TranslationUnit] = {}
    for n in names:
        for tu in views[n].tus:
            out.setdefault(cfg.map_source(tu.key()), tu)
    return out


def _tu_key(tu: TranslationUnit, cfg: "MigrationConfig", fold: bool) -> str:
    """Comparison key of a TU: its (config-mapped) source path, case-folded for
    Windows builds, where `Foo.cpp` and `foo.cpp` are one file."""
    key = cfg.map_source(tu.key())
    return key.lower() if fold else key


def _variants(views: Dict[str, TargetView], names, cfg: "MigrationConfig",
              fold: bool) -> Dict[str, List[TranslationUnit]]:
    """Pool the TUs of the given targets by source, keeping every DISTINCT flag
    set a source is compiled with. Most sources have one; a source shared by
    several targets can legitimately have more (MSBuild shared-items projects,
    a stub compiled into three DLLs with different warning levels), and
    comparing only whichever was seen first would leave the rest unverified."""
    out: Dict[str, List[TranslationUnit]] = {}
    for n in sorted(names):
        for tu in views[n].tus:
            bucket = out.setdefault(_tu_key(tu, cfg, fold), [])
            if not any(_same_flags(v, tu, fold) for v in bucket):
                bucket.append(tu)
    return out


def _same_flags(x: TranslationUnit, y: TranslationUnit, fold: bool) -> bool:
    if (x.defines, x.flags) != (y.defines, y.flags):
        return False
    if fold:
        return [i.lower() for i in x.includes] == [i.lower() for i in y.includes]
    return x.includes == y.includes


def _diff_variants(target: str, a_list: List[TranslationUnit],
                   b_list: List[TranslationUnit], cfg: "MigrationConfig",
                   fold: bool) -> List[Discrepancy]:
    """Compare every reference compile of a source against the Bazel compile of
    it that matches best (fewest errors). With one variant on each side this is
    exactly `_diff_tu`."""
    out: List[Discrepancy] = []
    seen = set()
    for a_tu in a_list:
        best = None
        for b_tu in b_list:
            found = _diff_tu(target, a_tu, b_tu, cfg, fold)
            score = (sum(1 for d in found if d.severity == Severity.ERROR.value), len(found))
            if best is None or score < best[0]:
                best = (score, found)
        for d in (best[1] if best else []):
            fingerprint = (d.kind, d.tu, tuple(d.cmake_only or ()), tuple(d.bazel_only or ()))
            if fingerprint not in seen:
                seen.add(fingerprint)
                out.append(d)
    if len(a_list) > len(b_list):
        out.append(Discrepancy(
            Kind.TU_VARIANT_COUNT.value, Severity.WARN.value, target,
            f"source compiled with {len(a_list)} distinct flag sets in the reference, "
            f"{len(b_list)} in bazel", tu=a_list[0].source))
    return out


# Java source roots: a .java path is keyed by its PACKAGE-rooted form (the path
# below the source root), which is invariant across build systems that disagree
# on the repo prefix / source-root layout (Bazel 'guava/src/...' vs Maven
# 'src/...' vs 'src/main/java/...'). We strip the longest matching root marker.
_JAVA_SRC_ROOTS = ("src/main/java/", "src/test/java/", "javatests/", "java/", "src/")


def _java_src_key(path: str) -> str:
    """Package-rooted key for a .java source, robust to differing repo/source-root
    prefixes. 'guava/src/com/x/A.java' and 'src/com/x/A.java' both -> 'com/x/A.java'.
    Falls back to the basename if no known root marker is present."""
    for root in _JAVA_SRC_ROOTS:
        i = path.find(root)
        if i != -1:
            return path[i + len(root):]
    return path


def _union_java_sources(views: Dict[str, TargetView], names) -> Dict[str, str]:
    """Pool every Java source across all compile groups of the given targets into
    one map: package-rooted key -> original path. Mirrors _union_tus: grouping
    (Maven's 2 groups vs Bazel's 1) is irrelevant; the SOURCE SET is what must
    match. First occurrence wins for the displayed original path."""
    out: Dict[str, str] = {}
    for n in names:
        for g in views[n].compile_groups:
            if g.language != "java":
                continue
            for src in g.sources:
                out.setdefault(_java_src_key(src), src)
    return out


def _is_generated_cs(path: str) -> bool:
    """A C# source the build system wrote for itself (assembly info, global
    usings, projections): under an intermediate directory, or outside the repo
    altogether. Each build generates its own, so they are not compared."""
    low = path.lower()
    return (low.startswith(("bazel-out/", "obj/", "@", "/")) or "/obj/" in low
            or (len(low) > 1 and low[1] == ":"))


_TFM = re.compile(r"^(net\d+\.\d+|net\d{2,3}|netstandard\d\.\d|netcoreapp\d\.\d)(-[a-z]+[\d.]*)?$",
                  re.IGNORECASE)


def _base_tfm(*candidates: str) -> str:
    """Target framework named by a path component or a `Name[tfm]` suffix,
    without its platform part: `net8.0-windows10.0.26100.0` is `net8.0`.
    rules_dotnet has no platform frameworks; the platform shows up in the
    defines and references instead, which are compared."""
    for text in candidates:
        for part in re.split(r"[\\/\[\]|]", text or ""):
            m = _TFM.match(part.strip())
            if m:
                return m.group(1).lower()
    return ""


@dataclass
class _Assembly:
    name: str               # assembly name as written
    tfm: str                # base target framework ('' if unknown)
    target: str             # the target that compiles it
    group: object           # reconstruct.CompileGroup

    @property
    def label(self) -> str:
        return "%s [%s]" % (self.name, self.tfm) if self.tfm else self.name


def _csharp_assemblies(views: Dict[str, TargetView], names) -> Dict[tuple, _Assembly]:
    out: Dict[tuple, _Assembly] = {}
    for n in sorted(names):
        for g in views[n].compile_groups:
            if g.language != "csharp":
                continue
            name = (g.csc.assembly if g.csc is not None else "") or ""
            if not name and g.key != "<csc>":
                name = g.key.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            name = name or n.split("[", 1)[0].rsplit(":", 1)[-1]
            tfm = _base_tfm(g.key, n)
            out.setdefault((name.lower(), tfm), _Assembly(name, tfm, n, g))
    return out


def _pair_assemblies(a: Dict[tuple, _Assembly], b: Dict[tuple, _Assembly]):
    """(pairs, only_a, only_b). Exact (name, framework) first; then by name
    alone where that is unambiguous, so one side not saying its framework
    does not unpair an assembly."""
    pairs, used_b = [], set()
    by_name: Dict[str, List[tuple]] = {}
    for key in b:
        by_name.setdefault(key[0], []).append(key)
    a_by_name: Dict[str, List[tuple]] = {}
    for key in a:
        a_by_name.setdefault(key[0], []).append(key)
    only_a = []
    for key in sorted(a):
        if key in b:
            pairs.append((a[key], b[key])); used_b.add(key)
        elif len(a_by_name[key[0]]) == 1 and len(by_name.get(key[0], [])) == 1 \
                and "" in (key[1], by_name[key[0]][0][1]):
            other = by_name[key[0]][0]
            pairs.append((a[key], b[other])); used_b.add(other)
        else:
            only_a.append(a[key])
    return pairs, only_a, [b[k] for k in sorted(b) if k not in used_b]


_BAZEL_OUT = re.compile(r"^bazel-out/[^/]+/bin/")


def _map_cs_source(src: str, cfg: "MigrationConfig") -> str:
    """`source_dir_map` and `source_map` for a C# source, ignoring case. These
    are Windows builds: MSBuild spells a directory the way the project file
    does (`previewpane/Common`), Bazel the way the file system does
    (`previewpane/common`), and both mean the same file."""
    src = cfg.map_source_dir(src, fold=True)
    low = src.lower()
    best = None
    for frm, to in cfg.source_map:
        if low.startswith(frm.lower()) and (best is None or len(frm) > len(best[0])):
            best = (frm, to)
    return best[1] if best else src


def _option_identity(option: str) -> str:
    """A compiler option whose value is a file (`/win32manifest:<path>`),
    compared by which file it is rather than by where each build keeps it."""
    key, sep, value = option.partition(":")
    if sep and key.lstrip("/") in csc_dialect.PATH_VALUED:
        return "%s:%s" % (key, _file_identity(value))
    return option


def _cs_source_key(src: str, cfg: "MigrationConfig") -> Optional[str]:
    """Comparison key of a C# source, or None if it is a file the build system
    wrote for itself and no config rule says how the two sides' copies
    correspond (each build generates its own assembly info; those are checked
    by content, not by path)."""
    # rules_dotnet builds each target framework in its own configuration, so a
    # generated file's directory carries an opaque hash
    # (`bazel-out/x64_windows-opt-ST-89742bf2ae83/bin/...`). Config rules name
    # it the way the `bazel-bin` link does.
    src = _BAZEL_OUT.sub("bazel-bin/", src.replace("\\", "/"))
    mapped = _map_cs_source(src, cfg)
    if mapped != src:
        return mapped.lower()
    low = src.replace("\\", "/").lower()
    if low.startswith(("@", "external/", "/")) or (len(low) > 1 and low[1] == ":"):
        # A source from outside the repository -- shipped in a package, like a
        # test SDK's entry point: the same file whichever directory each build
        # keeps its packages in.
        return "<external>/" + low.rsplit("/", 1)[-1]
    if _is_generated_cs(src):
        return None
    return low


def _file_identity(path: str) -> str:
    """Identity of a non-source compile input (additional file, analyzer
    config) across builds: its repo path, or just its name when it lives in a
    package or was generated."""
    p = path.replace("\\", "/")
    low = p.lower()
    generated = low.startswith(("bazel-out/", "obj/")) or "/obj/" in low
    if generated and low.endswith(".editorconfig"):
        return "<generated>.editorconfig"
    if generated or low.startswith(("@", "external/", "/")) or (len(low) > 1 and low[1] == ":"):
        return low.rsplit("/", 1)[-1]
    return low


def _binary_identities(paths, raw) -> Dict[str, Optional[tuple]]:
    """assembly file name -> (package id, version) or None, for a reference or
    analyzer list. `raw` are the same entries as written (package directories
    are still visible there)."""
    out: Dict[str, Optional[tuple]] = {}
    for path, original in zip(paths, raw):
        out.setdefault(csc_dialect.assembly_identity(path), csc_dialect.package_of(original))
    return out


_PACK_ALIASES = (
    # The installed .NET Framework targeting pack and the package that copies it.
    (re.compile(r"^microsoft\.netframework\.referenceassemblies(\.net\d+)?$"),
     "microsoft.netframework.referenceassemblies"),
    (re.compile(r"^netstandard\.library(\.ref)?$"), "netstandard.library"),
)


def _pack_family(package_id: str) -> str:
    """A framework pack's id without what differs between the copy a machine
    has installed and the package a build downloads."""
    low = package_id.lower()
    for pattern, family in _PACK_ALIASES:
        if pattern.match(low):
            return family
    return low


def _referenced_twice(paths) -> Dict[str, List[str]]:
    """assembly file name -> the different files passed under it, for names
    that were passed more than once."""
    seen: Dict[str, List[str]] = {}
    for path in paths:
        files = seen.setdefault(csc_dialect.assembly_identity(path), [])
        if path not in files:
            files.append(path)
    return {name: files for name, files in seen.items() if len(files) > 1}


def _extra_binaries(a_ids, b_ids, by_pack: bool) -> List[str]:
    """Files only the bazel side passes. With `by_pack`, those of a framework
    pack are counted per pack instead of listed: a rule set that passes a whole
    reference pack where MSBuild passes part of it adds a hundred at once."""
    names: List[str] = []
    packs: Dict[tuple, int] = {}
    for name in sorted(set(b_ids) - set(a_ids)):
        origin = b_ids[name]
        if by_pack and origin and csc_dialect.is_framework_pack(origin[0]):
            packs[origin] = packs.get(origin, 0) + 1
        else:
            names.append(name)
    return names + ["%d more assemblies of %s %s" % (count, origin[0], origin[1])
                    for origin, count in sorted(packs.items())]


def _diff_binaries(kind: Kind, what: str, label: str, a_ids, b_ids,
                   by_pack: bool = False, ambiguous=()) -> List[Discrepancy]:
    """`ambiguous` are names the bazel side passes from more than one file:
    reported by the caller, and not asked here which of the files is right."""
    missing = sorted(set(a_ids) - set(b_ids))
    wrong = []
    for name in sorted(set(a_ids) & set(b_ids)):
        pa, pb = a_ids[name], b_ids[name]
        if not pa or not pb or name in ambiguous:
            continue
        packs = csc_dialect.is_framework_pack(pa[0]) or csc_dialect.is_framework_pack(pb[0])
        if packs and _pack_family(pa[0]) != _pack_family(pb[0]):
            # Which pack an assembly comes from is not a version question:
            # `WindowsBase.dll` of the base pack is a facade, WindowsDesktop's
            # is the real one.
            wrong.append("%s: reference takes it from %s, bazel from %s" % (name, pa[0], pb[0]))
        elif not packs and pa != pb:
            wrong.append("%s: reference %s %s, bazel %s %s" % (name, pa[0], pa[1], pb[0], pb[1]))
    extra = _extra_binaries(a_ids, b_ids, by_pack)
    if not missing and not wrong:
        if not extra:
            return []
        # Neither kind of extra is inert. A reference adds names to bind to (a
        # second copy of a type makes a call ambiguous, a namespace it declares
        # can shadow the one the source means); a source generator adds code.
        return [Discrepancy(kind.value, Severity.WARN.value, label,
                            "%s only on the bazel side" % what, cmake_only=[],
                            bazel_only=extra)]
    show = lambda n: "%s (%s %s)" % (n, *a_ids[n]) if a_ids[n] else n
    return [Discrepancy(
        kind.value, Severity.ERROR.value, label,
        "%s missing on the bazel side, or taken from a different package version" % what,
        cmake_only=[show(n) for n in missing] + wrong, bazel_only=extra)]


def _diff_assembly(a: _Assembly, b: _Assembly, cfg: "MigrationConfig") -> List[Discrepancy]:
    out: List[Discrepancy] = []
    label = a.label
    # sources
    a_src = {k: s for s in a.group.sources for k in [_cs_source_key(s, cfg)] if k}
    b_src = {k: s for s in b.group.sources for k in [_cs_source_key(s, cfg)] if k}
    for key in sorted(set(a_src) - set(b_src)):
        out.append(Discrepancy(Kind.MISSING_CS_SRC.value, Severity.ERROR.value, label,
                               "C# source compiled into this assembly in the reference "
                               "but not in bazel", tu=a_src[key]))
    for key in sorted(set(b_src) - set(a_src)):
        out.append(Discrepancy(Kind.EXTRA_CS_SRC.value, Severity.WARN.value, label,
                               "C# source compiled into this assembly in bazel but not "
                               "in the reference", tu=b_src[key]))
    unpaired = sorted(s for s in a.group.sources if _cs_source_key(s, cfg) is None)
    if unpaired:
        # Not an error (the bazel build writes its own), but not nothing: an
        # assembly-info file carries attributes, a projection carries types.
        out.append(Discrepancy(
            Kind.MISSING_CS_SRC.value, Severity.WARN.value, label,
            "generated C# sources of the reference build that no source_map rule pairs "
            "with a bazel file: not compared", cmake_only=unpaired, bazel_only=[]))
    va, vb = a.group.csc, b.group.csc
    if va is None or vb is None:
        side = "reference" if va is None else "bazel"
        out.append(Discrepancy(
            Kind.FLAGS_DIFF.value, Severity.WARN.value, label,
            "the %s build recorded no compiler command line for this assembly, so only "
            "its source set was compared" % side))
        return out
    # defines
    a_def = {d for d in va.defines if not cfg.define_ignored(d)}
    b_def = {d for d in vb.defines if not cfg.define_ignored(d)}
    if a_def - b_def:
        out.append(Discrepancy(Kind.DEFINES_DIFF.value, Severity.ERROR.value, label,
                               "defines missing on the bazel side",
                               cmake_only=sorted(a_def - b_def), bazel_only=sorted(b_def - a_def)))
    elif b_def - a_def:
        out.append(Discrepancy(Kind.DEFINES_DIFF.value, Severity.WARN.value, label,
                               "defines only on the bazel side", cmake_only=[],
                               bazel_only=sorted(b_def - a_def)))
    # options
    a_opt = {_option_identity(o) for o in va.options if not cfg.flag_ignored(o)}
    b_opt = {_option_identity(o) for o in vb.options if not cfg.flag_ignored(o)}
    stray = _changed_defaults(a_opt, b_opt, csc_dialect.default_family)
    if a_opt - b_opt:
        out.append(Discrepancy(Kind.FLAGS_DIFF.value, Severity.ERROR.value, label,
                               "compiler options missing on the bazel side",
                               cmake_only=sorted(a_opt - b_opt), bazel_only=sorted(b_opt - a_opt)))
    elif stray:
        out.append(Discrepancy(Kind.FLAGS_DIFF.value, Severity.ERROR.value, label,
                               "bazel changes a compiler default the reference keeps",
                               cmake_only=[], bazel_only=stray))
    # references and analyzers
    # The same assembly name from two files: the compiler binds the name to
    # one of them, and it need not be the one the reference build passed
    # (a framework's facade `WindowsBase.dll` next to the real one).
    twice = _referenced_twice(vb.references)
    for name in set(_referenced_twice(va.references)):
        twice.pop(name, None)
    out.extend(_diff_binaries(Kind.CS_REFS_DIFF, "references", label,
                              _binary_identities(va.references, va.raw_references),
                              _binary_identities(vb.references, vb.raw_references),
                              by_pack=True, ambiguous=twice))
    for name in sorted(twice):
        out.append(Discrepancy(
            Kind.CS_REFS_DIFF.value, Severity.ERROR.value, label,
            "bazel passes two different files for one assembly name; the reference "
            "build passes one", cmake_only=[], bazel_only=[name] + sorted(twice[name])))
    out.extend(_diff_binaries(Kind.CS_ANALYZERS_DIFF, "analyzers or source generators", label,
                              _binary_identities(va.analyzers, va.raw_analyzers),
                              _binary_identities(vb.analyzers, vb.raw_analyzers)))
    # resources and the other files that steer the compile
    def inputs(v):
        return ({"resource:" + name for name, _ in v.resources}
                | {"additionalfile:" + _file_identity(p) for p in v.additional_files}
                | {"analyzerconfig:" + _file_identity(p) for p in v.analyzer_configs})
    a_in, b_in = inputs(va), inputs(vb)
    if a_in - b_in:
        out.append(Discrepancy(Kind.CS_RESOURCES_DIFF.value, Severity.ERROR.value, label,
                               "embedded resources, additional files or analyzer configs "
                               "missing on the bazel side",
                               cmake_only=sorted(a_in - b_in), bazel_only=sorted(b_in - a_in)))
    return out


def _diff_csharp(a_views: Dict[str, TargetView], b_views: Dict[str, TargetView],
                 cfg: "MigrationConfig") -> List[Discrepancy]:
    roles = set(PARTICIPATING_ROLES) | ({TargetRole.TEST} if cfg.include_tests else set())
    a_names = {n for n, t in a_views.items() if t.role in roles and not cfg.target_excluded(n)}
    # A test is not always a test rule on the bazel side, and the pairing is
    # by assembly, so every bazel C# compile is a candidate.
    b_names = {n for n in b_views if not cfg.target_excluded(n)}
    pairs, only_a, only_b = _pair_assemblies(_csharp_assemblies(a_views, a_names),
                                             _csharp_assemblies(b_views, b_names))
    out: List[Discrepancy] = []
    for asm in only_a:
        out.append(Discrepancy(Kind.MISSING_TARGET.value, Severity.ERROR.value, asm.label,
                               "C# assembly in the reference but not bazel"))
    for asm in only_b:
        out.append(Discrepancy(Kind.EXTRA_TARGET.value, Severity.WARN.value, asm.label,
                               "C# assembly in bazel but not the reference"))
    for a, b in pairs:
        out.extend(_diff_assembly(a, b, cfg))
    return out


def _union_csharp_sources(views: Dict[str, TargetView], names) -> Dict[str, str]:
    """Pool every hand-written C# source across the given targets: lower-cased
    repo-relative path -> path as written. Same idea as Java -- the SOURCE SET
    must match, however it is split into assemblies."""
    out: Dict[str, str] = {}
    for n in names:
        for g in views[n].compile_groups:
            if g.language != "csharp":
                continue
            for src in g.sources:
                if not _is_generated_cs(src):
                    out.setdefault(src.lower(), src)
    return out


def _all_source_keys(views: Dict[str, TargetView], cfg: "MigrationConfig",
                     fold: bool = False) -> set:
    """Every source key compiled ANYWHERE in the participating scope, ignoring
    role. Used as the presence reference so a source that's a library TU on one
    side and a test/exe TU on the other isn't falsely reported missing.

    Scope mirrors what the diff actually compares: all participating-role
    targets, plus test targets when cfg.include_tests. Config-excluded targets
    are omitted (they're intentionally out of scope)."""
    keys = set()
    for name, t in views.items():
        if cfg.target_excluded(name):
            continue
        in_scope = (t.role in PARTICIPATING_ROLES
                    or (cfg.include_tests and t.role == TargetRole.TEST))
        if not in_scope or _out_of_scope_language(t, cfg):
            continue
        for tu in t.tus:
            keys.add(_tu_key(tu, cfg, fold))
    return keys


def diff_models(a: CanonicalModel, b: CanonicalModel,
                cfg: Optional["MigrationConfig"] = None) -> List[Discrepancy]:
    out: List[Discrepancy] = []
    if cfg is None:
        cfg = MigrationConfig()
    # Reconstruct raw actions into comparable views (TUs + link flags + deps),
    # build-system-specific interpretation keyed on each model's tag.
    a_views = reconstruct(a)
    b_views = _apply_target_map(reconstruct(b), cfg.target_map)
    # A test is not always a test RULE on the Bazel side: a CppUnitTest DLL is a
    # `cc_binary` that a separate rule hands to vstest, and aquery cannot tell
    # it from a product DLL. A reviewed target_map entry that pairs it with a
    # reference test target says what it is, so it takes that role.
    for name in cfg.target_map:
        ta, tb = a_views.get(name), b_views.get(name)
        if ta is not None and tb is not None and ta.role == TargetRole.TEST \
                and tb.role == TargetRole.PRODUCTION:
            tb.role = TargetRole.TEST
    # C# targets are paired by assembly and compared on their own (_diff_csharp);
    # the native alignment below would only report them a second time.
    a_names = {n for n in _participating(a_views, cfg) if a_views[n].language != "c#"}
    b_names = {n for n in _participating(b_views, cfg) if b_views[n].language != "c#"}

    # Windows builds: the same file can be spelled with different case on the
    # two sides (and tracking logs upper-case everything), so paths compare
    # case-insensitively whenever either side is MSBuild.
    fold = BuildSystem.MSBUILD in (a.build_system, b.build_system)

    out.extend(_diff_toolchain(a, b, cfg))

    # Split production targets into libraries (compared as a TU-set union, so
    # grouping/naming is irrelevant) and executables (the real link
    # deliverables, compared by identity). Shared libraries are pooled unless
    # cfg.align_shared_libs asks for them to be aligned like executables.
    lib_kinds = _LIBRARY_KINDS - {TargetKind.SHARED} if cfg.align_shared_libs \
        else _LIBRARY_KINDS
    a_libs = {n for n in a_names if a_views[n].kind in lib_kinds}
    b_libs = {n for n in b_names if b_views[n].kind in lib_kinds}
    a_exes = a_names - a_libs
    b_exes = b_names - b_libs

    # PRESENCE REFERENCE: every source compiled ANYWHERE on each side, across all
    # participating roles (libraries, executables, and tests when opted in). A
    # source is only "missing" if compiled nowhere on the other side -- the same
    # .cc may be a library TU on one side and a test/exe TU on the other (e.g.
    # Bazel compiling a test helper into a `testonly` cc_library while CMake
    # builds it straight into the test exe). Role grouping must not manufacture
    # a false missing/extra. Flag comparison still happens per-union below.
    a_all = _all_source_keys(a_views, cfg, fold)
    b_all = _all_source_keys(b_views, cfg, fold)

    # Every Bazel compile in scope, by source. A reference TU is compared with
    # the Bazel compile of the SAME source wherever Bazel put it: grouping a
    # binary's sources into a library must not exempt their flags from the diff.
    b_scope = set(b_names)
    if cfg.include_tests:
        b_scope |= {n for n, t in b_views.items()
                    if t.role == TargetRole.TEST and not cfg.target_excluded(n)
                    and not _out_of_scope_language(t, cfg)}
    b_anywhere = _variants(b_views, b_scope, cfg, fold)

    # ---- libraries: project-wide TU-set comparison -------------------------
    a_union = _variants(a_views, a_libs, cfg, fold)
    b_union = _variants(b_views, b_libs, cfg, fold)
    # What to print for a source: the comparison key, except that a case-folded
    # key is shown as the path was actually written.
    show = (lambda key, tu: tu.source) if fold else (lambda key, tu: key)

    for src in sorted(set(a_union) - b_all):
        out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                               "<libraries>", "source compiled in cmake but not bazel",
                               tu=show(src, a_union[src][0])))
    for src in sorted(set(b_union) - a_all):
        out.append(Discrepancy(Kind.EXTRA_TU.value, Severity.WARN.value,
                               "<libraries>", "source compiled in bazel but not cmake",
                               tu=show(src, b_union[src][0])))
    for src in sorted(a_union):
        candidates = b_union.get(src) or b_anywhere.get(src)
        if candidates:
            out.extend(_diff_variants("<libraries>", a_union[src], candidates, cfg, fold))

    # ---- executables: identity-aligned, own TUs + own flags ----------------
    what = lambda views, n: ("shared library" if views[n].kind == TargetKind.SHARED
                             else "executable")
    for name in sorted(a_exes - b_exes):
        out.append(Discrepancy(Kind.MISSING_TARGET.value, Severity.ERROR.value,
                               name, what(a_views, name) + " in cmake but not bazel"))
    for name in sorted(b_exes - a_exes):
        out.append(Discrepancy(Kind.EXTRA_TARGET.value, Severity.WARN.value,
                               name, what(b_views, name) + " in bazel but not cmake"))
    for name in sorted(a_exes & b_exes):
        ta, tb = a_views[name], b_views[name]
        # TU keys go through cfg.map_source (see _union_tus) so codegen grouping
        # asymmetries collapse to a shared token on both sides.
        amap = {_tu_key(tu, cfg, fold): tu for tu in ta.tus}
        bmap = {_tu_key(tu, cfg, fold): tu for tu in tb.tus}
        # Presence is judged against the whole other side (a_all/b_all), not just
        # this exe's own TUs: a source this exe compiles may live in a library on
        # the other side (or vice versa) -- that's a grouping difference, not a
        # missing source. Only a source compiled NOWHERE on the other side is a
        # real gap.
        for src in sorted(set(amap) - b_all):
            out.append(Discrepancy(Kind.MISSING_TU.value, Severity.ERROR.value,
                                   name, "source compiled in cmake but not bazel",
                                   tu=show(src, amap[src])))
        for src in sorted(set(bmap) - a_all):
            out.append(Discrepancy(Kind.EXTRA_TU.value, Severity.WARN.value,
                                   name, "source compiled in bazel but not cmake",
                                   tu=show(src, bmap[src])))
        for src in sorted(amap):
            if src in bmap:
                out.extend(_diff_tu(name, amap[src], bmap[src], cfg, fold))
            elif src in b_anywhere:
                out.extend(_diff_variants(name, [amap[src]], b_anywhere[src], cfg, fold))
        # link flags: asymmetric subset, same policy as compile flags -- every
        # CMake link flag must be present on the Bazel side; extra Bazel link
        # flags tolerated (toolchain link noise already stripped). Reviewer
        # ignores applied to both sides.
        out.extend(_diff_link_flags(name, ta, tb, cfg))

    # ---- Java: project-wide source-SET comparison --------------------------
    # Java compiles a whole source set per action; how those sources are grouped
    # into compile actions differs across builds (Maven 2 groups -- main +
    # multi-release -- vs Bazel 1). So, exactly like C++ libraries, we pool every
    # Java source across all groups into one set keyed by package-rooted path
    # (robust to repo/source-root prefix differences) and compare the sets.
    # NOTE: javac FLAG comparison is deferred (the Bazel side is a JavaBuilder
    # wrapper argv whose real javac flags are nested -- needs canonicalization
    # designed against this real data). Source-set parity is what's checked now.
    a_java = _union_java_sources(a_views, a_names)
    b_java = _union_java_sources(b_views, b_names)
    for key in sorted(set(a_java) - set(b_java)):
        out.append(Discrepancy(Kind.MISSING_JAVA_SRC.value, Severity.ERROR.value,
                               "<java>", "java source compiled in A but not B",
                               tu=a_java[key]))
    for key in sorted(set(b_java) - set(a_java)):
        out.append(Discrepancy(Kind.EXTRA_JAVA_SRC.value, Severity.WARN.value,
                               "<java>", "java source compiled in B but not A",
                               tu=b_java[key]))

    # ---- C#: per-assembly comparison (opt-in) ------------------------------
    # One csc invocation builds one assembly, and an assembly is a deliverable
    # (its name is what other code references), so assemblies are paired by
    # name and target framework and compared one to one: sources, defines,
    # options, references, analyzers, resources.
    if cfg.include_csharp:
        out.extend(_diff_csharp(a_views, b_views, cfg))

    # ---- link closure: EXTERNAL deps only ----------------------------------
    # Internal (in-project) dep names are meaningless once libraries are
    # dissolved into a TU union, but external/system libs (-lpthread, -lz, ...)
    # must still be present for the binary to link. Compare the project-wide
    # set of external deps; require every CMake external dep on the Bazel side.
    # A dep spelled differently per build (CMake's archive basename 'Catch2Main'
    # vs Bazel's 'catch2_main', or 'OpenSSL::SSL' vs 'ssl') is aligned by an
    # explicit, recorded cfg.dep_map entry -- not a fuzzy match -- so a residual
    # missing_dep is a genuinely-absent dep, not a naming artifact.
    # An "external" dep naming a target that EXISTS as an in-project target
    # (either side) is not truly external -- it's an internal lib the CMake
    # File-API surfaced as a link fragment (common with cyclically-linked
    # static libs, whose .a is repeated on the link line but which are not a
    # direct structural dependency). Such names are verified via the compile
    # TU-set of that target, so drop them from the external closure.
    _internal_names = set(a_views) | set(b_views)
    a_ext = {cfg.dep_map.get(d, d) for d in _external_deps(a_views, a_names)
             if cfg.dep_map.get(d, d) not in _internal_names and d not in _internal_names}
    b_ext = {d for d in _external_deps(b_views, b_names) if d not in _internal_names}
    for dep in sorted(a_ext - b_ext):
        out.append(Discrepancy(Kind.MISSING_DEP.value, Severity.ERROR.value,
                               "<external>", "external link dependency missing on bazel side",
                               cmake_only=[dep]))

    # ---- tests (opt-in) ----------------------------------------------------
    if cfg.include_tests:
        out.extend(_diff_tests(a_views, b_views, cfg, a_all, b_all, fold))
    return out


# Toolchain facts compared across sides. Version skew between two compilers
# makes a flag diff unreliable (different defaults, different diagnostics), so
# it is reported before anything else and blocks convergence unless accepted.
_TOOLCHAIN_KEYS = ("msvc_version", "winsdk_version", "target_arch")


# .NET facts: the SDK whose compiler ran (`dotnet_sdk_version`) and the version
# of each framework reference pack (`pack:<id>`). Checked only with C# in scope.
_DOTNET_KEY_PREFIXES = ("dotnet_", "pack:")


def _diff_toolchain(a: CanonicalModel, b: CanonicalModel,
                    cfg: "MigrationConfig") -> List[Discrepancy]:
    if not a.toolchain:
        return []
    out: List[Discrepancy] = []
    # native
    wanted = [k for k in _TOOLCHAIN_KEYS
              if a.toolchain.get(k) and k not in cfg.ignore_toolchain]
    if wanted and not any(b.toolchain.get(k) for k in _TOOLCHAIN_KEYS):
        # A C#-only bazel slice has no MSVC action; that is not a finding.
        if not any(k.startswith(_DOTNET_KEY_PREFIXES) for k in b.toolchain):
            out.append(Discrepancy(
                Kind.TOOLCHAIN_DIFF.value, Severity.WARN.value, "<toolchain>",
                "the bazel model records no toolchain (no MSVC compile action in the "
                "aquery?), so it cannot be checked against the reference",
                cmake_only=["%s=%s" % (k, a.toolchain[k]) for k in wanted]))
        wanted = []
    # .NET: a fact is compared when both builds have it. A pack only the
    # reference uses shows up as missing references, not here.
    if cfg.include_csharp:
        wanted += sorted(k for k in a.toolchain
                         if k.startswith(_DOTNET_KEY_PREFIXES) and b.toolchain.get(k)
                         and k not in cfg.ignore_toolchain)
    for k in wanted:
        ours, theirs = a.toolchain[k], b.toolchain.get(k, "")
        if ours.lower() != theirs.lower():
            out.append(Discrepancy(
                Kind.TOOLCHAIN_DIFF.value, Severity.ERROR.value, "<toolchain>",
                "%s differs: reference %s, bazel %s" % (k, ours, theirs or "unknown"),
                cmake_only=["%s=%s" % (k, ours)],
                bazel_only=["%s=%s" % (k, theirs)] if theirs else []))
    return out


def _diff_tests(a_views: Dict[str, TargetView], b_views: Dict[str, TargetView],
                cfg: "MigrationConfig", a_all: set, b_all: set,
                fold: bool = False) -> List[Discrepancy]:
    """Apply the COMPILE-PARITY stage to TEST targets. Two checks:

      1. TU-SET union of all test sources (like libraries): same test .cc files
         compiled with equivalent flags, regardless of how they're grouped into
         test binaries or named. Presence is judged against ALL sources on the
         other side (a_all/b_all), not just its test union: a test source on one
         side may be compiled into a (testonly) library on the other -- a
         grouping difference, not a missing source. Only "compiled nowhere" is
         flagged.
      2. Test-binary EXISTENCE by count: a coarse check that both sides produce
         a comparable number of test executables, catching the case where test
         sources compile but are never linked into runnable tests.

    SCOPE: tests get the COMPILE-PARITY stage only. LINK CONSISTENCY (per-binary
    link flags and link-dep closure) runs for PRODUCTION executables/shared
    libs, not for test binaries: tests align by source-set union, not by binary
    identity, so there's no per-binary link command to compare. So with tests
    on, "converged" means production artifacts are link-consistent AND all test
    SOURCES compile equivalently -- not that each test binary links identically.
    Extending link consistency to per-test-binary is a natural future increment.
    """
    out: List[Discrepancy] = []
    a_tests = {n for n, t in a_views.items()
               if t.role == TargetRole.TEST and not cfg.target_excluded(n)
               and t.language != "c#"}
    b_tests = {n for n, t in b_views.items()
               if t.role == TargetRole.TEST and not cfg.target_excluded(n)
               and t.language != "c#"}
    show = (lambda key, tu: tu.source) if fold else (lambda key, tu: key)

    # 1. test-source TU-set union
    a_union = _variants(a_views, a_tests, cfg, fold)
    b_union = _variants(b_views, b_tests, cfg, fold)
    for src in sorted(set(a_union) - b_all):
        out.append(Discrepancy(Kind.MISSING_TEST_TU.value, Severity.ERROR.value,
                               "<tests>", "test source compiled in cmake but not bazel",
                               tu=show(src, a_union[src][0])))
    for src in sorted(set(b_union) - a_all):
        out.append(Discrepancy(Kind.EXTRA_TEST_TU.value, Severity.WARN.value,
                               "<tests>", "test source compiled in bazel but not cmake",
                               tu=show(src, b_union[src][0])))
    for src in sorted(set(a_union) & set(b_union)):
        out.extend(_diff_variants("<tests>", a_union[src], b_union[src], cfg, fold))

    # 2. test-binary existence by count
    if len(a_tests) != len(b_tests):
        out.append(Discrepancy(
            Kind.TEST_BINARY_COUNT.value, Severity.WARN.value, "<tests>",
            f"test executable count differs: cmake={len(a_tests)} bazel={len(b_tests)}",
            cmake_only=sorted(a_tests), bazel_only=sorted(b_tests)))
    return out


def _external_deps(views: Dict[str, TargetView], names) -> set:
    return {d.name for n in names for d in views[n].deps if d.external}


def excluded_summary(a: CanonicalModel, b: CanonicalModel,
                     cfg: "MigrationConfig") -> dict:
    """Targets NOT compared, grouped by reason and side, for separate inspection.

    This is the visible record of what the parity diff skipped and why -- so a
    skipped dashboard/codegen/test target, or a config-excluded third-party
    subtree, is an explicit, reviewable line item rather than a silent omission.
    """
    # tests participate when opted in, so they are no longer "excluded" then
    participating = set(PARTICIPATING_ROLES)
    if cfg.include_tests:
        participating.add(TargetRole.TEST)

    def by_reason(model: CanonicalModel) -> dict:
        out: Dict[str, List[str]] = {}
        for name, t in sorted(reconstruct(model).items()):
            if cfg.target_excluded(name):
                out.setdefault("config_excluded", []).append(name)
            elif _out_of_scope_language(t, cfg):
                out.setdefault("csharp", []).append(name)
            elif t.role not in participating:
                out.setdefault(t.role.value, []).append(name)
        return out
    return {"cmake": by_reason(a), "bazel": by_reason(b)}


def summarize(discs: List[Discrepancy],
              a: Optional[CanonicalModel] = None,
              b: Optional[CanonicalModel] = None,
              cfg: Optional["MigrationConfig"] = None) -> dict:
    errors = [d for d in discs if d.severity == Severity.ERROR.value]
    result = {
        "total": len(discs),
        "errors": len(errors),
        "warnings": len(discs) - len(errors),
        "converged": len(errors) == 0,
        "discrepancies": [asdict(d) for d in discs],
    }
    if a is not None and b is not None:
        result["excluded"] = excluded_summary(a, b, cfg or MigrationConfig())
    return result


if __name__ == "__main__":
    import sys
    # Used as a CLI by the skill loop:
    #   diff.py <cmake.json> <bazel.json> [any2bazel.json]
    # The 3rd arg is the migration config (target_map + ignore lists). If
    # omitted, the diff runs with no human-approved suppressions.
    import config as config_mod
    from serialize import load_model
    a = load_model(sys.argv[1])
    b = load_model(sys.argv[2])
    cfg = config_mod.load(sys.argv[3]) if len(sys.argv) > 3 else MigrationConfig()
    print(json.dumps(summarize(diff_models(a, b, cfg), a, b, cfg), indent=2))
