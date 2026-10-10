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

"""Flag canonicalization for the MSVC dialect (cl.exe, link.exe, lib.exe and
their clang-cl / lld-link equivalents).

This is the MSVC counterpart of canonicalize.py and follows the same policy --
drop invocation mechanics, ignore build-type flags, keep everything that can
change what is built -- but it is a SEPARATE table on purpose. The two dialects
share spellings that mean different things: GNU `-MD`/`-MT` are dep-file
bookkeeping, MSVC `/MD`/`/MT` select the C runtime and are an ABI decision.
Grafting MSVC onto the GNU tables silently ignores the latter.

Dialect rules implemented here:

  * `/x` and `-x` are the same option; canonical form is `/x`.
  * `/D NAME` and `/DNAME` are the same; so are `/I dir` and `/Idir`.
  * Defines resolve the way the compiler resolves them: the LAST `/D` of a name
    wins and `/U` removes it. A toolchain default such as `_WIN32_WINNT=0x0601`
    followed by the project's own value therefore compares as the project's.
  * Single-state options (C runtime, warning level, `/GS`, `/Zc:wchar_t`, ...)
    resolve the same way -- last one wins -- and a state equal to the
    compiler's default is written as nothing. See `_FAMILY`.
  * Paths are Windows paths wherever they came from, handled with `ntpath` so
    the result is the same on any host. A relative path is relative to the
    action's working directory.
  * Linker option names are case-insensitive (`/dll` == `/DLL`).

An unknown flag is KEPT. Only flags listed here as mechanics or build-type are
dropped, so a correctness flag this file has never heard of still surfaces.
"""

from __future__ import annotations

import ntpath
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_COMPILERS = {"cl", "cl.exe", "clang-cl", "clang-cl.exe"}
_LINKERS = {"link", "link.exe", "lld-link", "lld-link.exe", "lib", "lib.exe",
            "llvm-lib", "llvm-lib.exe"}

_SOURCE_EXTS = (".c", ".cc", ".cpp", ".cxx", ".c++", ".ixx", ".cppm", ".asm", ".s")
_LINK_INPUT_EXTS = (".obj", ".o", ".lib", ".res", ".exp", ".a", ".lo", ".pdb", ".ilk")

# ---- cl.exe ---------------------------------------------------------------------

# Options whose value may be the NEXT argument (`/D NAME`) or joined (`/DNAME`).
_CL_VALUE_OPTIONS = ("/external:I", "/imsvc", "/D", "/U", "/I", "/FI", "/AI", "/FU",
                     "/Tp", "/Tc")

# Invocation mechanics: where outputs go, how the driver reports, precompiled
# header plumbing, static-analysis side channels. Exact tokens.
_CL_MECHANICS = {
    "/c", "/nologo", "/FS", "/FC", "/Gm", "/Gm-", "/JMC", "/JMC-", "/showIncludes",
    "/Brepro", "/Brepro-", "/Y-", "/FA", "/FAs", "/FAc", "/FAcs", "/FAu",
    "/experimental:deterministic", "/d1nodatetime",
}
# ... and prefixes (the value is joined).
_CL_MECHANICS_PREFIXES = (
    "/Fo", "/Fd", "/Fa", "/Fe", "/Fp", "/FR", "/Fr", "/Fi", "/Fm", "/Fx", "/doc",
    "/Yc", "/Yu", "/Yl", "/Zm", "/errorReport", "/diagnostics:", "/analyze",
    "/external:W", "/external:anglebrackets", "/external:templates",
    "/external:env:", "/sourceDependencies", "/pathmap:", "/ifcOutput",
    "/d1reportTime", "/Bt", "/d2cgsummary",
)
# Build-type flags (the MSVC analog of -O* / -g*): optimization and debug-info
# level, never a migration decision.
_CL_BUILD_TYPE = {
    "/Z7", "/Zi", "/ZI", "/Zo", "/Zo-", "/GL", "/GL-", "/Gy", "/Gy-", "/Gw", "/Gw-",
    "/GF", "/GF-", "/Zf",
}
_CL_BUILD_TYPE_PREFIXES = ("/O", "/RTC", "/ZH:")

_MP = re.compile(r"^/MP\d*$")
_DATE_MACROS = {"__DATE__", "__TIME__", "__TIMESTAMP__"}

# Options that set ONE state, where the last one on the line wins. They are
# resolved the way the compiler resolves them, for two reasons:
#
#   * MSBuild restates compiler defaults on every command line (`/Zc:wchar_t
#     /Zc:forScope /Gd /fp:precise /GS`). A side that says nothing has exactly
#     the same build, so a default-valued state is written as nothing.
#   * A toolchain default overridden later on the same line (`/MD ... /MT`)
#     must compare as the override. Treating flags as a plain set would see
#     `/MD` present and call a changed C runtime a match.
#
# token -> (family, state). _FAMILY_DEFAULT gives the state that is the
# compiler's own default (and so is elided); a family not listed there has no
# default worth eliding and always shows its effective state.
_FAMILY = {
    "/GS": ("GS", "/GS"), "/GS-": ("GS", "/GS-"),
    "/GR": ("GR", "/GR"), "/GR-": ("GR", "/GR-"),
    "/WX": ("WX", "/WX"), "/WX-": ("WX", "/WX-"),
    "/Zc:wchar_t": ("wchar_t", "/Zc:wchar_t"), "/Zc:wchar_t-": ("wchar_t", "/Zc:wchar_t-"),
    "/Zc:forScope": ("forScope", "/Zc:forScope"), "/Zc:forScope-": ("forScope", "/Zc:forScope-"),
    "/Gd": ("callconv", "/Gd"), "/Gr": ("callconv", "/Gr"), "/Gz": ("callconv", "/Gz"),
    "/Gv": ("callconv", "/Gv"),
    "/fp:precise": ("fp", "/fp:precise"), "/fp:fast": ("fp", "/fp:fast"),
    "/fp:strict": ("fp", "/fp:strict"),
    "/MD": ("crt", "/MD"), "/MDd": ("crt", "/MDd"), "/MT": ("crt", "/MT"),
    "/MTd": ("crt", "/MTd"),
    "/W0": ("W", "/W0"), "/W1": ("W", "/W1"), "/W2": ("W", "/W2"), "/W3": ("W", "/W3"),
    "/W4": ("W", "/W4"), "/Wall": ("W", "/Wall"),
    "/TP": ("lang", "/TP"), "/TC": ("lang", "/TC"),
}
_FAMILY_DEFAULT = {"GS": "/GS", "GR": "/GR", "WX": "/WX-", "wchar_t": "/Zc:wchar_t",
                   "forScope": "/Zc:forScope", "callconv": "/Gd", "fp": "/fp:precise"}
_STD_PREFIX = "/std:"          # `/std:c++17`, `/std:c11`: last one wins too
_C_EXTS = (".c",)


def default_family(flag: str) -> Optional[str]:
    """For a canonical flag that states a NON-default state of an option
    family the compiler has a default for (`/GR-`, `/GS-`, `/WX`, `/fp:fast`):
    the family. A side that says nothing about the family has the default, so
    such a flag on one side only is a difference whichever side it is on."""
    entry = _FAMILY.get(flag)
    if entry and entry[0] in _FAMILY_DEFAULT:
        return entry[0]
    return None


def _basename(tok: str) -> str:
    return ntpath.basename(tok.replace("/", "\\")).lower()


def is_msvc_compile(args: Sequence[str]) -> bool:
    return bool(args) and _basename(args[0]) in _COMPILERS


def is_msvc_link(args: Sequence[str]) -> bool:
    return bool(args) and _basename(args[0]) in _LINKERS


def _looks_like_posix_path(tok: str) -> bool:
    """`/work/proj/a.cpp` on a POSIX host is a path, not an option. Only
    matters for fixtures and non-Windows hosts: a token with a second slash and
    a source/object extension that does not start a known joined-value option."""
    if not tok.startswith("/") or tok.count("/") < 2 or ":" in tok:
        return False
    low = tok.lower()
    if not low.endswith(_SOURCE_EXTS + _LINK_INPUT_EXTS + (".h", ".hpp", ".def")):
        return False
    return not tok.startswith(_CL_VALUE_OPTIONS + _CL_MECHANICS_PREFIXES)


def _is_option(tok: str) -> bool:
    if tok.startswith("-"):
        return len(tok) > 1
    return tok.startswith("/") and len(tok) > 1 and not _looks_like_posix_path(tok)


def _slash(tok: str) -> str:
    """Canonical option prefix."""
    return "/" + tok[1:] if tok.startswith("-") else tok


def norm_path(path: str, repo_root: str = "", cwd: str = "",
              path_map: Iterable[Tuple[str, str]] = ()) -> str:
    """Normalize a path from an MSVC command line to a comparable form:
    an external-root token (`@nuget/x/include`), else repo-relative POSIX,
    else an absolute path with forward slashes.

    `cwd` is the action's working directory (absolute, or relative to the repo
    root, or empty for the repo root itself).
    """
    p = path.strip().strip('"')
    if not p:
        return p
    p = p.replace("/", "\\")
    if not _is_abs(p):
        base = cwd.replace("/", "\\") if cwd else ""
        if base and not _is_abs(base) and repo_root:
            base = ntpath.join(repo_root.replace("/", "\\"), base)
        if base:
            p = ntpath.join(base, p)
    p = ntpath.normpath(p)
    if len(p) > 3:
        p = p.rstrip("\\")
    low = p.lower()
    for root, token in path_map:
        r = ntpath.normpath(root.replace("/", "\\")).rstrip("\\").lower()
        if low == r or low.startswith(r + "\\"):
            rest = p[len(r):].replace("\\", "/").strip("/")
            return token + ("/" + rest if rest else "")
    if repo_root:
        r = ntpath.normpath(repo_root.replace("/", "\\")).rstrip("\\").lower()
        if low == r:
            return "."
        if low.startswith(r + "\\"):
            return p[len(r) + 1:].replace("\\", "/")
    return p.replace("\\", "/")


def _is_abs(p: str) -> bool:
    # Windows drive/UNC absolute, or POSIX absolute (fixtures, other hosts).
    return bool(ntpath.splitdrive(p)[0]) or p.startswith("\\")


def _split_value(tok: str, nxt: Optional[str]) -> Optional[Tuple[str, str, int]]:
    """If `tok` is a value-taking cl option, return (option, value, consumed)
    where consumed is 1 (joined) or 2 (value was the next argument)."""
    for opt in _CL_VALUE_OPTIONS:
        if tok == opt:
            return (opt, nxt, 2) if nxt is not None else (opt, "", 1)
        if tok.startswith(opt):
            # `/Tp`/`/Tc` are exact two-letter options: do not swallow /TP, /TC.
            return opt, tok[len(opt):], 1
    return None


def cl_source(args: Sequence[str]) -> Optional[str]:
    """The source a compile command line compiles: the `/Tp`/`/Tc` argument or
    the (last) positional argument. None if there is none."""
    src = None
    start = 1 if args and not _is_option(args[0]) and is_msvc_compile(args) else 0
    i, n = start, len(args)
    while i < n:
        tok = args[i]
        if not _is_option(tok):
            src = tok
            i += 1
            continue
        tok = _slash(tok)
        if tok == "/link":
            break
        hit = _split_value(tok, args[i + 1] if i + 1 < n else None)
        if hit:
            opt, value, used = hit
            if opt in ("/Tp", "/Tc") and value:
                src = value
            i += used
            continue
        i += 1
    return src


def cl_include_dirs(args: Sequence[str]) -> List[str]:
    """Include directories of a compile command line exactly as written
    (`/I`, `/external:I`), in order. For callers that need the raw paths."""
    out: List[str] = []
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if not _is_option(tok):
            i += 1
            continue
        hit = _split_value(_slash(tok), args[i + 1] if i + 1 < n else None)
        if hit:
            opt, value, used = hit
            if opt in ("/I", "/external:I", "/imsvc") and value:
                out.append(value)
            i += used
            continue
        i += 1
    return out


def canonicalize_cl_flags(
        raw: Sequence[str], repo_root: str, *, cwd: str = "",
        path_map: Iterable[Tuple[str, str]] = (),
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """(defines, includes, other flags) of one cl.exe compile, canonical.

    - defines: effective set (last /D wins, /U removes), sorted
    - includes: /I and /external:I directories, normalized, order preserved
    - other: every remaining flag that is not mechanics or build type, sorted
    """
    path_map = list(path_map)
    defines: Dict[str, str] = {}
    includes: List[str] = []
    other: List[str] = []
    state: Dict[str, str] = {}          # family -> effective (last) state
    std: Dict[str, str] = {}            # "c" / "c++" -> effective /std:

    start = 1 if raw and not _is_option(raw[0]) and is_msvc_compile(raw) else 0
    i, n = start, len(raw)
    while i < n:
        tok = raw[i]
        if not _is_option(tok):
            i += 1                      # positional: the source (or an object)
            continue
        tok = _slash(tok)
        if tok == "/link":
            break                       # the rest is for the linker
        hit = _split_value(tok, raw[i + 1] if i + 1 < n else None)
        if hit:
            opt, value, used = hit
            i += used
            if opt == "/D":
                # cl accepts NAME#VALUE as NAME=VALUE.
                if "=" not in value and "#" in value:
                    value = value.replace("#", "=", 1)
                name = value.split("=", 1)[0]
                if name and name not in _DATE_MACROS:
                    defines.pop(name, None)     # keep insertion order = last write
                    defines[name] = value
            elif opt == "/U":
                defines.pop(value, None)
                other.append("/U" + value)
            elif opt in ("/I", "/external:I", "/imsvc"):
                if value:
                    includes.append(norm_path(value, repo_root, cwd, path_map))
            elif opt == "/FI":
                # A bare name is found through the include path, not the cwd.
                if "\\" in value or "/" in value:
                    value = norm_path(value, repo_root, cwd, path_map)
                other.append("/FI " + value)
            elif opt in ("/AI", "/FU"):
                other.append(opt + " " + norm_path(value, repo_root, cwd, path_map))
            # /Tp and /Tc name the source: mechanics.
            continue
        i += 1
        if tok in _CL_MECHANICS or tok.startswith(_CL_MECHANICS_PREFIXES) or _MP.match(tok):
            continue
        if tok in _CL_BUILD_TYPE or tok.startswith(_CL_BUILD_TYPE_PREFIXES):
            continue
        if tok in _FAMILY:
            family, value = _FAMILY[tok]
            state[family] = value
            continue
        if tok.startswith(_STD_PREFIX):
            # C and C++ standards are separate settings (`/std:c11 /std:c++17`).
            std["c++" if "c++" in tok else "c"] = tok
            continue
        other.append(tok)

    # `/TP` / `/TC` force a language. MSBuild writes `/TP` on every C++ file
    # and Bazel relies on the extension; only a language that DIFFERS from what
    # the extension implies is a fact about the build.
    lang = state.pop("lang", None)
    if lang:
        source = (cl_source(raw) or "").lower()
        implied = "/TC" if source.endswith(_C_EXTS) else "/TP"
        if not source or lang != implied:
            other.append(lang)
    for family, value in state.items():
        if _FAMILY_DEFAULT.get(family) != value:
            other.append(value)
    other.extend(std.values())

    canon_includes: List[str] = []
    for inc in includes:
        if not canon_includes or canon_includes[-1] != inc:
            canon_includes.append(inc)
    return (tuple(sorted(defines.values())), tuple(canon_includes),
            tuple(sorted(set(other))))


# ---- link.exe / lib.exe ---------------------------------------------------------

# Dropped: outputs, search paths, reporting, debugger side files.
_LINK_MECHANICS = {
    "OUT", "NOLOGO", "PDB", "PDBSTRIPPED", "PDBALTPATH", "IMPLIB", "ILK", "LTCGOUT",
    "MAP", "MAPINFO", "ERRORREPORT", "TLBID", "TLBOUT", "IDLOUT", "MANIFESTFILE",
    "NATVIS", "SOURCELINK", "INCREMENTAL", "BREPRO", "LIBPATH", "WINMDFILE", "PGD",
    "DEBUGTYPE", "TIME", "VERBOSE", "EXPERIMENTAL",
}
# Build type: debug info and optimization.
_LINK_BUILD_TYPE = {"DEBUG", "OPT", "LTCG", "PROFILE", "RELEASE", "GENPROFILE",
                    "FASTGENPROFILE", "USEPROFILE"}
# These name a library to link: part of the link closure, not a flag.
_LINK_LIBRARY_OPTIONS = {"DEFAULTLIB", "WHOLEARCHIVE"}
# Value is a file path.
_LINK_PATH_VALUES = {"DEF", "MANIFESTINPUT", "ORDER", "STUB", "KEYFILE"}
# Value is a file or library name compared case-insensitively.
_LINK_LOWER_VALUES = {"DELAYLOAD", "NODEFAULTLIB"}
# Value is a keyword compared case-insensitively.
_LINK_UPPER_VALUES = {"SUBSYSTEM", "MANIFEST", "GUARD", "WINMD", "MACHINE", "DYNAMICBASE",
                      "NXCOMPAT", "CETCOMPAT", "LARGEADDRESSAWARE", "HIGHENTROPYVA",
                      "TSAWARE", "SAFESEH", "APPCONTAINER", "FORCE", "CLRIMAGETYPE",
                      "MANIFESTUAC", "ALLOWISOLATION", "INTEGRITYCHECK", "FIXED"}
# Options that set ONE state: the last one on the line wins, as in link.exe
# (checked with dumpbin: `/SUBSYSTEM:CONSOLE /SUBSYSTEM:WINDOWS` is a GUI image,
# `/MANIFEST:EMBED /MANIFEST:NO` embeds nothing). Bazel's MSVC toolchain puts
# `/SUBSYSTEM:CONSOLE` on every link, so a project's `/SUBSYSTEM:WINDOWS` only
# compares correctly when resolved this way. Everything else accumulates.
_LINK_SINGLE_STATE = {
    "SUBSYSTEM", "MACHINE", "MANIFEST", "MANIFESTUAC", "DYNAMICBASE", "NXCOMPAT",
    "CETCOMPAT", "LARGEADDRESSAWARE", "HIGHENTROPYVA", "TSAWARE", "SAFESEH",
    "APPCONTAINER", "ALLOWISOLATION", "INTEGRITYCHECK", "FIXED", "DEF", "ENTRY",
    "BASE", "STACK", "HEAP", "VERSION", "ALIGN", "FILEALIGN", "STUB", "ORDER",
    "KEYFILE", "WINMD", "CLRIMAGETYPE", "DRIVER", "GUARD",
}
# States link.exe has without being told (same check: a bare link sets "Dynamic
# base" and "NX compatible", writes no manifest and no CFG table). MSBuild
# restates the first two on every line; a side that is silent has the same image.
# `/MANIFEST` is NOT here: a bare link.exe writes no manifest at all.
_LINK_DEFAULTS = {"/DYNAMICBASE", "/NXCOMPAT", "/MANIFEST:NO", "/GUARD:NO",
                  # What `/MANIFEST:EMBED` alone puts in the manifest; MSBuild spells it out.
                  "/MANIFESTUAC:LEVEL='ASINVOKER' UIACCESS='FALSE'"}
# Search paths are mechanics, with one exception: a `lib\spectre\<arch>` directory
# ahead of the default one swaps in the Spectre-mitigated C runtime and STL.
# Nothing else on the command line says so (`/Qspectre` only covers the
# project's own code), so the fact is kept as one token, whatever the root.
_SPECTRE_LIBS = re.compile(r"(^|[\\/])lib[\\/]spectre([\\/]|$)", re.IGNORECASE)
SPECTRE_LIBS_FLAG = "/LIBPATH:@msvc/lib/spectre"


def _link_option(tok: str) -> Tuple[str, Optional[str]]:
    body = tok[1:]
    if ":" in body:
        name, value = body.split(":", 1)
        return name.upper(), value
    return body.upper(), None


def canonicalize_link_flags(
        raw: Sequence[str], repo_root: str = "", *, cwd: str = "",
        path_map: Iterable[Tuple[str, str]] = (),
) -> Tuple[str, ...]:
    """Correctness-relevant flags of one link.exe command line, canonical
    (`/NAME[:value]`, name upper-cased), sorted. Inputs and library-naming
    options are the link closure and are returned by `link_libraries`."""
    path_map = list(path_map)
    out: List[str] = []
    state: Dict[str, str] = {}          # single-state option -> effective (last) token
    start = 1 if raw and not _is_option(raw[0]) else 0
    for tok in raw[start:]:
        if not _is_option(tok) or tok.startswith("@"):
            continue
        name, value = _link_option(_slash(tok))
        if name in _LINK_MECHANICS or name in _LINK_BUILD_TYPE or name in _LINK_LIBRARY_OPTIONS:
            if name == "WHOLEARCHIVE" and value is None:
                out.append("/WHOLEARCHIVE")
            if name == "LIBPATH" and value and _SPECTRE_LIBS.search(value.strip('"')):
                out.append(SPECTRE_LIBS_FLAG)
            continue
        if value is None:
            canon = "/" + name
        else:
            value = value.strip('"')
            if name in _LINK_PATH_VALUES:
                value = norm_path(value, repo_root, cwd, path_map)
            elif name in _LINK_LOWER_VALUES:
                value = value.lower()
            elif name in _LINK_UPPER_VALUES:
                value = value.upper()
            canon = "/%s:%s" % (name, value)
        if name == "GUARD" and canon != "/GUARD:NO":
            # `/guard:cf` and `/guard:ehcont` are separate protections; only
            # `/guard:no` resets them.
            state.pop("GUARD", None)
            out.append(canon)
        elif name in _LINK_SINGLE_STATE:
            if name == "GUARD":
                out = [o for o in out if not o.startswith("/GUARD:")]
            state[name] = canon
        else:
            out.append(canon)
    out.extend(v for v in state.values() if v not in _LINK_DEFAULTS)
    return tuple(sorted(set(out)))


def link_libraries(raw: Sequence[str]) -> List[str]:
    """Libraries a link (or lib) command line names, as written: positional
    `.lib` arguments plus `/DEFAULTLIB:` and `/WHOLEARCHIVE:` values."""
    libs: List[str] = []
    start = 1 if raw and not _is_option(raw[0]) and is_msvc_link(raw) else 0
    for tok in raw[start:]:
        if _is_option(tok):
            name, value = _link_option(_slash(tok))
            if name in _LINK_LIBRARY_OPTIONS and value:
                libs.append(value.strip('"'))
            continue
        if tok.lower().endswith(".lib"):
            libs.append(tok)
    return libs


def link_output(raw: Sequence[str]) -> str:
    """The `/OUT:` value of a link or lib command line ('' if absent)."""
    for tok in raw:
        if _is_option(tok):
            name, value = _link_option(_slash(tok))
            if name == "OUT" and value:
                return value.strip('"')
    return ""


def lib_identity(lib: str) -> str:
    """Abstract dependency name of a Windows library: file name without the
    `.lib` extension, lower-cased (the filesystem is case-insensitive, so
    `Advapi32.lib` and `advapi32.lib` are one library). A Bazel interface
    library `foo.if.lib` is `foo`. The name is NOT cut at the first dot --
    `Microsoft.Management.Deployment.lib` is a normal Windows library name --
    and a `lib` prefix is kept (`libcurl.lib` is not `curl.lib`)."""
    base = ntpath.basename(lib.replace("/", "\\")).lower()
    for suffix in (".if.lib", ".lib", ".dll", ".a"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base
