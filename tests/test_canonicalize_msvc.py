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

"""MSVC-dialect canonicalization: what cl.exe / link.exe flags mean, as far as
a parity check cares. All paths are Windows paths handled with ntpath, so these
run identically on any host."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from canonicalize_msvc import (canonicalize_cl_flags, canonicalize_link_flags,
                               cl_include_dirs, cl_source, is_msvc_compile,
                               lib_identity, link_libraries, link_output, norm_path)

REPO = "C:\\work\\proj"
CWD = "C:\\work\\proj\\src\\lib"
CL = "C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\CL.exe"


def cl(*flags, source="a.cpp", cwd=CWD, path_map=()):
    return canonicalize_cl_flags([CL, "/c", *flags, source], REPO, cwd=cwd, path_map=path_map)


def test_dash_and_slash_spell_the_same_option():
    assert cl("-EHsc", "-DFOO") == cl("/EHsc", "/DFOO")


def test_define_forms_and_hash_as_equals():
    defines, _, _ = cl("/D", "A", "/DB=1", "/DC#2")
    assert defines == ("A", "B=1", "C=2")


def test_last_define_wins_like_the_compiler():
    # Bazel's toolchain default followed by the project's own value.
    defines, _, _ = cl("/D_WIN32_WINNT=0x0601", "/D_WIN32_WINNT=0x0A00")
    assert defines == ("_WIN32_WINNT=0x0A00",)


def test_undefine_removes_the_define_and_stays_visible():
    defines, _, flags = cl("/DNDEBUG", "/UNDEBUG")
    assert defines == ()
    assert "/UNDEBUG" in flags


def test_includes_resolve_against_the_action_cwd():
    _, includes, _ = cl("/I..\\", "/I..\\..\\", "/IC:\\work\\proj\\include\\", "/I", "Public")
    assert includes == ("src", ".", "include", "src/lib/Public")


def test_external_roots_become_tokens():
    path_map = [("C:\\work\\proj\\packages\\Foo.1.2.3", "@nuget/foo"),
                ("C:\\Program Files (x86)\\Windows Kits\\10", "@winsdk")]
    _, includes, _ = cl("/IC:\\work\\proj\\packages\\Foo.1.2.3\\build\\native\\..\\..\\include\\",
                        "/external:IC:\\PROGRAM FILES (X86)\\WINDOWS KITS\\10\\Include\\um",
                        path_map=path_map)
    # `..` is folded before matching, and the match ignores case.
    assert includes == ("@nuget/foo/include", "@winsdk/Include/um")


def test_mechanics_and_build_type_are_dropped():
    _, _, flags = cl("/nologo", "/FS", "/MP", "/MP8", "/Fo" + "x64\\Release\\", "/FdC:\\out\\a.pdb",
                     "/Fpx64\\pch.pch", "/Ycpch.h", "/Yupch.h", "/Zm500", "/showIncludes", "/FC",
                     "/Gm-", "/errorReport:queue", "/diagnostics:column", "/analyze-",
                     "/analyze:ruleset" + "C:\\x y\\r.ruleset", "/external:W0",
                     "/external:anglebrackets", "/O2", "/Oi", "/Od", "/Z7", "/Zi", "/GL", "/Gy",
                     "/RTC1")
    assert flags == ()


def test_correctness_flags_survive():
    _, _, flags = cl("/EHsc", "/permissive-", "/utf-8", "/Zc:inline", "/await:strict", "/sdl",
                     "/Qspectre", "/guard:cf", "/bigobj", "/wd4996", "/std:c++17")
    assert set(flags) == {"/EHsc", "/permissive-", "/utf-8", "/Zc:inline", "/await:strict",
                          "/sdl", "/Qspectre", "/guard:cf", "/bigobj", "/wd4996", "/std:c++17"}


def test_runtime_library_is_not_dep_file_noise():
    # GNU -MD/-MT are dep-file bookkeeping; MSVC /MD and /MT select the CRT.
    assert "/MD" in cl("/MD")[2]
    assert "/MT" in cl("/MT")[2]


def test_last_runtime_library_wins():
    # A toolchain default overridden later on the line must compare as the
    # override -- as a plain set, `/MD` would still look present.
    assert [f for f in cl("/MD", "/MT")[2] if f.startswith("/M")] == ["/MT"]


def test_restated_compiler_defaults_are_not_flags():
    # MSBuild writes these on every command line; saying nothing is the same build.
    assert cl("/Zc:wchar_t", "/Zc:forScope", "/Gd", "/fp:precise", "/GS", "/GR", "/WX-")[2] == ()


def test_non_default_states_are_flags():
    _, _, flags = cl("/Zc:wchar_t-", "/Gz", "/fp:fast", "/GS-", "/GR-", "/WX", "/W4")
    assert set(flags) == {"/Zc:wchar_t-", "/Gz", "/fp:fast", "/GS-", "/GR-", "/WX", "/W4"}


def test_language_flag_only_counts_when_it_contradicts_the_extension():
    assert "/TP" not in cl("/TP", source="a.cpp")[2]      # what the extension says anyway
    assert "/TC" not in cl("/TC", source="a.c")[2]
    assert "/TP" in cl("/TP", source="a.c")[2]            # a .c file compiled as C++
    assert "/TC" in cl("/TC", source="a.cpp")[2]


def test_unknown_flags_are_kept_not_dropped():
    assert "/someFutureFlag:on" in cl("/someFutureFlag:on")[2]


def test_forced_include_bare_name_is_not_resolved_against_cwd():
    # /FI"pch.h" is found through the include path.
    assert "/FI pch.h" in cl("/FIpch.h")[2]
    assert "/FI src/lib/sub/x.h" in cl("/FIsub\\x.h")[2]


def test_date_macros_are_dropped():
    assert cl('/D__DATE__="redacted"', "/DREAL")[0] == ("REAL",)


def test_source_is_the_positional_argument_wherever_it_is():
    assert cl_source([CL, "/c", "/DX", "version.cpp"]) == "version.cpp"
    # Bazel puts it right after /c, with more flags following.
    assert cl_source(["C:/vs/cl.exe", "/nologo", "/c", "src/a.cpp", "/Fobazel-out/a.obj"]) \
        == "src/a.cpp"
    assert cl_source([CL, "/c", "/D", "NOT_A_SOURCE", "/Tp", "weird.txt"]) == "weird.txt"
    assert cl_source([CL, "/c", "/DX"]) is None


def test_include_dirs_raw():
    assert cl_include_dirs([CL, "/I", "a", "/Ib\\c", "/external:I", "d", "/DX"]) == ["a", "b\\c", "d"]


def test_is_msvc_compile():
    assert is_msvc_compile([CL, "/c"])
    assert is_msvc_compile(["external/llvm/bin/clang-cl.exe", "/c"])
    assert not is_msvc_compile(["/usr/bin/clang", "-c"])


def test_bazel_style_relative_paths_stay_relative():
    defines, includes, _ = canonicalize_cl_flags(
        ["C:/vs/cl.exe", "/nologo", "/DFOO", "/I.", "/Ibazel-out/x64_windows-opt/bin",
         "/Iexternal/nuget_foo/include", "/c", "src/lib/a.cpp"], REPO)
    assert includes == (".", "bazel-out/x64_windows-opt/bin", "external/nuget_foo/include")
    assert defines == ("FOO",)


# ---- link ---------------------------------------------------------------------

LINK = "C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\link.exe"


def test_link_flags_keep_what_shapes_the_binary():
    flags = canonicalize_link_flags(
        [LINK, "/ERRORREPORT:QUEUE", "/OUT:C:\\work\\proj\\x64\\Release\\a.dll", "/INCREMENTAL:NO",
         "/NOLOGO", "/LIBPATH:C:\\work\\proj\\vcpkg\\lib", "/NATVIS:C:\\x\\wil.natvis",
         "kernel32.lib", "/DEF:Source.def", "/DELAYLOAD:WINSQLITE3.DLL", "/MANIFEST",
         "/manifest:embed", "/DEBUG:FULL", "/PDB:C:\\o\\a.pdb", "/SUBSYSTEM:WINDOWS", "/OPT:REF",
         "/LTCG:incremental", "/LTCGOUT:x64\\a.iobj", "/TLBID:1", "/WINMD:NO", "/DYNAMICBASE",
         "/NXCOMPAT", "/IMPLIB:C:\\o\\a.lib", "/MACHINE:X64", "/CETCOMPAT", "/guard:cf", "/dll",
         "x64\\Release\\a.obj"], REPO, cwd=CWD)
    # `/MANIFEST /manifest:embed` is one decision (embed); `/DYNAMICBASE` and
    # `/NXCOMPAT` restate what link.exe does anyway.
    assert flags == ("/CETCOMPAT", "/DEF:src/lib/Source.def", "/DELAYLOAD:winsqlite3.dll",
                     "/DLL", "/GUARD:CF", "/MACHINE:X64", "/MANIFEST:EMBED",
                     "/SUBSYSTEM:WINDOWS", "/WINMD:NO")


def test_link_single_state_options_resolve_like_the_linker():
    # Bazel's toolchain default first, the project's choice after it.
    assert canonicalize_link_flags(
        [LINK, "/SUBSYSTEM:CONSOLE", "/MACHINE:X64", "/SUBSYSTEM:WINDOWS"]) == (
        "/MACHINE:X64", "/SUBSYSTEM:WINDOWS")
    # ... and the other way round is a different binary, which must show.
    assert canonicalize_link_flags(
        [LINK, "/SUBSYSTEM:WINDOWS", "/SUBSYSTEM:CONSOLE"]) == ("/SUBSYSTEM:CONSOLE",)
    # Turning a default off is kept; turning it back on is the default again.
    assert canonicalize_link_flags([LINK, "/DYNAMICBASE:NO"]) == ("/DYNAMICBASE:NO",)
    assert canonicalize_link_flags([LINK, "/DYNAMICBASE:NO", "/DYNAMICBASE"]) == ()
    assert canonicalize_link_flags([LINK, "/MANIFEST:EMBED", "/MANIFEST:NO"]) == ()
    # A bare link.exe writes no manifest, so `/MANIFEST` is a decision.
    assert canonicalize_link_flags([LINK, "/MANIFEST"]) == ("/MANIFEST",)
    assert canonicalize_link_flags([LINK, "/MANIFESTUAC:NO"]) == ("/MANIFESTUAC:NO",)
    # MSBuild writes out the UAC block the linker would embed anyway.
    assert canonicalize_link_flags(
        [LINK, "/MANIFEST:EMBED", "/MANIFESTUAC:level='asInvoker' uiAccess='false'"]) == (
        "/MANIFEST:EMBED",)


def test_spectre_library_path_is_kept_as_one_fact():
    assert canonicalize_link_flags(
        [LINK, "/LIBPATH:C:/VS/VC/Tools/MSVC/14.51.36231/lib/spectre/x64",
         "/LIBPATH:C:/VS/VC/Tools/MSVC/14.51.36231/lib/spectre/onecore/x64",
         "/LIBPATH:C:/repo/vcpkg_installed/x64/lib"]) == ("/LIBPATH:@msvc/lib/spectre",)
    assert canonicalize_link_flags([LINK, "/LIBPATH:C:/libs/respectre/x64"]) == ()


def test_link_guard_options_accumulate_until_reset():
    assert canonicalize_link_flags([LINK, "/guard:cf", "/guard:ehcont"]) == (
        "/GUARD:CF", "/GUARD:EHCONT")
    assert canonicalize_link_flags([LINK, "/guard:cf", "/guard:no"]) == ()
    assert canonicalize_link_flags([LINK, "/guard:no", "/guard:cf"]) == ("/GUARD:CF",)


def test_link_multi_valued_options_accumulate():
    assert canonicalize_link_flags(
        [LINK, "/DELAYLOAD:a.dll", "/DELAYLOAD:B.dll", "/NODEFAULTLIB:libcmt"]) == (
        "/DELAYLOAD:a.dll", "/DELAYLOAD:b.dll", "/NODEFAULTLIB:libcmt")


def test_link_option_names_are_case_insensitive():
    assert canonicalize_link_flags([LINK, "/dll", "-SubSystem:console"]) == (
        "/DLL", "/SUBSYSTEM:CONSOLE")


def test_link_libraries_and_output():
    args = [LINK, "/OUT:C:\\o\\a.dll", "Advapi32.lib", "/DEFAULTLIB:ucrt.lib",
            "C:\\vcpkg\\lib\\*.lib", "/WHOLEARCHIVE:bazel-out\\foo.lib", "x64\\a.obj",
            "x64\\version.res", "/NODEFAULTLIB:libucrt.lib"]
    assert link_libraries(args) == ["Advapi32.lib", "ucrt.lib", "C:\\vcpkg\\lib\\*.lib",
                                    "bazel-out\\foo.lib"]
    assert link_output(args) == "C:\\o\\a.dll"
    # Library-naming options are the link closure, not flags.
    assert canonicalize_link_flags(args) == ("/NODEFAULTLIB:libucrt.lib",)


def test_lib_identity_is_windows_naming():
    assert lib_identity("Advapi32.lib") == "advapi32"                  # case-insensitive FS
    assert lib_identity("C:\\x\\Microsoft.Management.Deployment.lib") \
        == "microsoft.management.deployment"                          # dots are part of the name
    assert lib_identity("libcurl.lib") == "libcurl"                    # no `lib` prefix rule
    assert lib_identity("bazel-out/x64/bin/foo.if.lib") == "foo"       # Bazel interface library


def test_norm_path_forms():
    assert norm_path("C:\\work\\proj", REPO) == "."
    assert norm_path("C:\\WORK\\PROJ\\Src\\a.cpp", REPO) == "Src/a.cpp"
    assert norm_path("C:\\elsewhere\\x.h", REPO) == "C:/elsewhere/x.h"
    assert norm_path("x64\\Release\\Generated Files\\", REPO, CWD) \
        == "src/lib/x64/Release/Generated Files"
    # cwd may itself be repo-relative.
    assert norm_path("..\\inc", REPO, "src/lib") == "src/inc"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
