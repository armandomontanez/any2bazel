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

"""The differ on MSVC-dialect builds: an MSBuild reference model against a
Bazel model whose toolchain is cl.exe.

Argv shapes are the real ones on each side: MSBuild runs in the project
directory with absolute and project-relative paths and the source last; Bazel
runs at the workspace root with relative paths, `/c <source>` mid-line and its
toolchain's default flags in front.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from config import MigrationConfig
from diff import diff_models, excluded_summary
from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TargetRole)

REPO = "C:\\work\\proj"
CL = "C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\CL.exe"
LINK = "C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\link.exe"
BAZEL_CL = "C:/VS/VC/Tools/MSVC/14.44.35207/bin/HostX64/x64/cl.exe"
BAZEL_LINK = "C:/VS/VC/Tools/MSVC/14.44.35207/bin/HostX64/x64/link.exe"
# What rules_cc's MSVC toolchain puts on every compile line.
BAZEL_DEFAULTS = ["/nologo", "/DNOMINMAX", "/D_WIN32_WINNT=0x0601", "/D_CRT_SECURE_NO_WARNINGS",
                  "/bigobj", "/Zm500", "/EHsc", "/wd4996"]
TOOLCHAIN = {"msvc_version": "14.44.35207", "winsdk_version": "10.0.26100.0",
             "target_arch": "x64"}


def msbuild_compile(project_dir, source, *flags):
    cwd = REPO + "\\" + project_dir
    return Action("CppCompile", (CL, "/c", *flags, source), inputs=(cwd + "\\" + source,), cwd=cwd)


def bazel_compile(source, *flags):
    return Action("CppCompile", (BAZEL_CL, *BAZEL_DEFAULTS, *flags, "/c", source,
                                 "/Fobazel-out/x64_windows-opt/bin/_objs/x.obj"))


def reference(*targets, toolchain=TOOLCHAIN, path_map=()):
    m = CanonicalModel(build_system=BuildSystem.MSBUILD, repo_root=REPO,
                       toolchain=dict(toolchain), path_map=list(path_map))
    for t in targets:
        m.add(t)
    return m


def bazel(*targets, toolchain=TOOLCHAIN):
    m = CanonicalModel(build_system=BuildSystem.BAZEL, repo_root=REPO, toolchain=dict(toolchain))
    for t in targets:
        m.add(t)
    return m


def lib(name, *actions, kind=TargetKind.STATIC, role=TargetRole.PRODUCTION, **attrs):
    return Target(name, kind, actions=list(actions), role=role, attrs=dict(attrs))


def errors(discs):
    return [d for d in discs if d.severity == "error"]


def test_equivalent_builds_converge_despite_different_spellings():
    a = reference(lib("core", msbuild_compile(
        "src\\core", "Thing.cpp", "/IC:\\work\\proj\\src\\core\\Public\\", "/I..\\common",
        "/D", "NDEBUG", "/D", "UNICODE", "/EHsc", "/MD", "/W4", "/std:c++17", "/permissive-",
        "/GS", "/Zc:wchar_t", "/Gd", "/fp:precise", "/TP", "/O2", "/Zi", "/Fox64\\Release\\",
        "/Yupch.h", "/FC", "/errorReport:queue", "/nologo")))
    b = bazel(lib(":core", bazel_compile(
        "src/core/thing.cpp", "/Isrc/core/public", "/Isrc/common", "/I.", "/DNDEBUG", "/DUNICODE",
        "/MD", "/W4", "/std:c++17", "/permissive-")))
    assert errors(diff_models(a, b)) == []


def test_source_and_include_paths_compare_case_insensitively():
    a = reference(lib("core", msbuild_compile("SRC\\Core", "THING.CPP", "/IPUBLIC")))
    b = bazel(lib(":core", bazel_compile("src/core/thing.cpp", "/Isrc/core/Public")))
    assert errors(diff_models(a, b)) == []


def test_missing_correctness_flag_is_caught():
    a = reference(lib("core", msbuild_compile("src", "a.cpp", "/permissive-", "/Qspectre")))
    b = bazel(lib(":core", bazel_compile("src/a.cpp", "/permissive-")))
    (d,) = errors(diff_models(a, b))
    assert (d.kind, d.cmake_only) == ("flags_diff", ["/Qspectre"])
    assert d.tu == "src/a.cpp"


def test_a_default_changed_on_the_bazel_side_is_not_an_extra_flag():
    # The reference keeps RTTI and the security cookie (it says nothing, or
    # restates the default). Bazel turning them off is not "one more flag".
    a = reference(lib("core", msbuild_compile("src", "a.cpp", "/GS", "/W4")))
    for flag in ("/GR-", "/GS-", "/fp:fast"):
        b = bazel(lib(":core", bazel_compile("src/a.cpp", "/W4", flag)))
        (d,) = errors(diff_models(a, b))
        assert (d.kind, d.cmake_only, d.bazel_only) == ("flags_diff", [], [flag])
    # A flag that belongs to no such family is still a tolerated extra.
    b = bazel(lib(":core", bazel_compile("src/a.cpp", "/W4", "/utf-8")))
    assert errors(diff_models(a, b)) == []


def test_runtime_library_mismatch_is_caught_even_when_both_spellings_appear():
    # The Bazel line carries the toolchain's /MD and then the BUILD file's /MT.
    # The compiler honours the last one; so must the diff.
    a = reference(lib("core", msbuild_compile("src", "a.cpp", "/MD")))
    b = bazel(lib(":core", bazel_compile("src/a.cpp", "/MD", "/MT")))
    (d,) = errors(diff_models(a, b))
    assert (d.kind, d.cmake_only) == ("flags_diff", ["/MD"])
    assert "/MT" in d.bazel_only and "/MD" not in d.bazel_only


def test_define_overridden_after_a_toolchain_default_matches():
    a = reference(lib("core", msbuild_compile("src", "a.cpp", "/D", "_WIN32_WINNT=0x0A00")))
    b = bazel(lib(":core", bazel_compile("src/a.cpp", "/D_WIN32_WINNT=0x0A00")))
    discs = diff_models(a, b)
    assert errors(discs) == []
    # The toolchain's other defaults are still visible, as a warning.
    (d,) = [d for d in discs if d.kind == "defines_diff"]
    assert "NOMINMAX" in d.bazel_only and "_WIN32_WINNT=0x0601" not in d.bazel_only


def test_toolchain_default_left_in_place_is_a_real_difference():
    a = reference(lib("core", msbuild_compile("src", "a.cpp", "/D", "_WIN32_WINNT=0x0A00")))
    b = bazel(lib(":core", bazel_compile("src/a.cpp")))
    (d,) = errors(diff_models(a, b))
    assert (d.kind, d.cmake_only) == ("defines_diff", ["_WIN32_WINNT=0x0A00"])


def test_external_roots_align_through_tokens():
    path_map = [("C:\\work\\proj\\packages\\Foo.1.2.3", "@nuget/foo")]
    a = reference(lib("core", msbuild_compile(
        "src", "a.cpp", "/IC:\\work\\proj\\packages\\Foo.1.2.3\\build\\native\\..\\..\\include\\")),
        path_map=path_map)
    b = bazel(lib(":core", bazel_compile("src/a.cpp", "/Iexternal/+nuget+foo/include")))
    (d,) = errors(diff_models(a, b))
    assert d.cmake_only == ["@nuget/foo/include"]
    cfg = MigrationConfig(include_map=(("external/+nuget+foo", "@nuget/foo"),))
    assert errors(diff_models(a, b, cfg)) == []


def test_every_flag_variant_of_a_shared_source_is_checked():
    # One stub compiled into two libraries, once with /W4 and once with /W3.
    a = reference(
        lib("one", msbuild_compile("src\\one", "..\\shared\\stub.c", "/W4", "/DONE")),
        lib("two", msbuild_compile("src\\two", "..\\shared\\stub.c", "/W3", "/DTWO")))
    both = bazel(lib(":one", bazel_compile("src/shared/stub.c", "/W4", "/DONE")),
                 lib(":two", bazel_compile("src/shared/stub.c", "/W3", "/DTWO")))
    assert errors(diff_models(a, both)) == []
    # Bazel compiling it only one way leaves the other variant unsatisfied.
    once = bazel(lib(":one", bazel_compile("src/shared/stub.c", "/W4", "/DONE")))
    discs = diff_models(a, once)
    assert {(d.kind, tuple(d.cmake_only or ())) for d in errors(discs)} == {
        ("defines_diff", ("TWO",)), ("flags_diff", ("/W3",))}
    assert any(d.kind == "tu_variant_count" and d.severity == "warn" for d in discs)


def test_binary_sources_grouped_into_a_bazel_library_are_still_compared():
    # MSBuild compiles main.cpp in the exe project; Bazel puts it in a cc_library
    # the cc_binary depends on. The flags must be checked all the same.
    exe = lambda: Target("tool", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION)
    a_exe = exe()
    a_exe.actions = [msbuild_compile("src\\tool", "main.cpp", "/permissive-"),
                     Action("CppLink", (LINK, "/SUBSYSTEM:CONSOLE"))]
    b_exe = Target("tool", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                   actions=[Action("CppLink", (BAZEL_LINK, "/SUBSYSTEM:CONSOLE"))])
    b_lib = lib(":tool_lib", bazel_compile("src/tool/main.cpp"))
    (d,) = errors(diff_models(reference(a_exe), bazel(b_exe, b_lib)))
    assert (d.kind, d.target, d.cmake_only) == ("flags_diff", "tool", ["/permissive-"])


def _dll(name, link_argv, compile_action=None):
    actions = [compile_action] if compile_action else []
    return Target(name, TargetKind.SHARED, role=TargetRole.PRODUCTION,
                  actions=actions + [Action("CppLink", tuple(link_argv))])


def test_shared_libraries_are_pooled_unless_alignment_is_requested():
    a = reference(_dll("Engine", [LINK, "/DLL", "/DEF:Source.def", "/DELAYLOAD:gdi32.dll"],
                       msbuild_compile("src\\engine", "e.cpp")))
    # Bazel builds the same source into a plain library; no DLL at all.
    b = bazel(lib(":engine", bazel_compile("src/engine/e.cpp")))
    assert errors(diff_models(a, b)) == []
    discs = errors(diff_models(a, b, MigrationConfig(align_shared_libs=True)))
    assert [(d.kind, d.target) for d in discs] == [("missing_target", "Engine")]
    assert "shared library" in discs[0].detail


def test_aligned_shared_library_link_flags_are_compared():
    a = reference(_dll("Engine", [LINK, "/OUT:C:\\o\\Engine.dll", "/dll", "/DEF:Source.def",
                                  "/DELAYLOAD:GDI32.DLL", "/DEBUG:FULL", "/OPT:REF",
                                  "/SUBSYSTEM:WINDOWS", "x64\\e.obj"]))
    a.targets["Engine"].actions[0].cwd = REPO + "\\src\\engine"
    b = bazel(_dll("Engine", [BAZEL_LINK, "/OUT:bazel-out/x64/bin/Engine.dll", "/DLL",
                              "/SUBSYSTEM:CONSOLE", "/SUBSYSTEM:WINDOWS", "/MACHINE:X64",
                              "/DEFAULTLIB:msvcrt.lib", "bazel-out/x64/bin/e.obj"]))
    cfg = MigrationConfig(align_shared_libs=True)
    (d,) = errors(diff_models(a, b, cfg))
    assert d.kind == "link_flags_diff"
    assert d.cmake_only == ["/DEF:src/engine/Source.def", "/DELAYLOAD:gdi32.dll"]


def test_system_library_deps_align_by_windows_naming():
    a_exe = Target("tool", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                   actions=[Action("CppLink", (LINK, "Advapi32.lib"))],
                   deps=[Dependency("advapi32", external=True, origin="winsdk"),
                         Dependency("wininet", external=True, origin="winsdk"),
                         Dependency("core", external=False)])
    b_exe = Target("tool", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                   actions=[Action("CppLink", (BAZEL_LINK, "/DEFAULTLIB:ADVAPI32.lib",
                                               "bazel-out/x64/bin/core.lib",
                                               "external/vcpkg/lib/libcurl.lib"))])
    (d,) = errors(diff_models(reference(a_exe), bazel(b_exe)))
    assert (d.kind, d.cmake_only) == ("missing_dep", ["wininet"])


def test_subsystem_is_the_last_one_on_the_line_not_any_one_present():
    # Bazel's toolchain says CONSOLE on every link. A project that asked for
    # WINDOWS first and lost to the default is a different binary.
    a = reference(_dll("Engine", [LINK, "/DLL", "/SUBSYSTEM:WINDOWS", "x64\\e.obj"]))
    won = bazel(_dll("Engine", [BAZEL_LINK, "/DLL", "/SUBSYSTEM:CONSOLE", "/SUBSYSTEM:WINDOWS"]))
    lost = bazel(_dll("Engine", [BAZEL_LINK, "/DLL", "/SUBSYSTEM:WINDOWS", "/SUBSYSTEM:CONSOLE"]))
    cfg = MigrationConfig(align_shared_libs=True)
    assert errors(diff_models(a, won, cfg)) == []
    (d,) = errors(diff_models(a, lost, cfg))
    assert (d.kind, d.cmake_only) == ("link_flags_diff", ["/SUBSYSTEM:WINDOWS"])


def test_spectre_mitigated_runtime_is_a_link_fact():
    # MSBuild's SpectreMitigation property shows up only as a library search
    # path; a Bazel link without one links the unmitigated C runtime.
    spectre = "/LIBPATH:C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\lib\\spectre\\x64"
    a = reference(_dll("Engine", [LINK, "/DLL", spectre, "/LIBPATH:C:\\v\\lib", "x64\\e.obj"]))
    cfg = MigrationConfig(align_shared_libs=True)
    without = bazel(_dll("Engine", [BAZEL_LINK, "/DLL", "/LIBPATH:external/v/lib"]))
    (d,) = errors(diff_models(a, without, cfg))
    assert (d.kind, d.cmake_only) == ("link_flags_diff", ["/LIBPATH:@msvc/lib/spectre"])
    with_ = bazel(_dll("Engine", [BAZEL_LINK, "/DLL", "/LIBPATH:external/+msvc_toolset+msvc/lib/spectre/x64"]))
    assert errors(diff_models(a, with_, cfg)) == []


def test_a_library_offered_as_an_input_is_not_a_library_linked():
    a_exe = Target("tool", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                   actions=[Action("CppLink", (LINK, "comsuppw.lib"))],
                   deps=[Dependency("comsuppw", external=True, origin="msvc")])
    # The whole Spectre directory is an input of the Bazel link; nothing names comsuppw.
    offered = ("external/msvc/lib/spectre/x64/comsuppw.lib", "external/msvc/lib/spectre/x64/msvcrt.lib")
    b_exe = Target("tool", TargetKind.EXECUTABLE, role=TargetRole.PRODUCTION,
                   actions=[Action("CppLink", (BAZEL_LINK, "/LIBPATH:external/msvc/lib/spectre/x64",
                                               "bazel-out/x64/bin/a.obj"), inputs=offered)])
    (d,) = errors(diff_models(reference(a_exe), bazel(b_exe)))
    assert (d.kind, d.cmake_only) == ("missing_dep", ["comsuppw"])
    # Without --include_param_files the command line is unknown; inputs are all there is.
    b_exe.actions[0] = Action("CppLink", (BAZEL_LINK, "@bazel-out/x64/bin/tool.exe-2.params"),
                              inputs=offered)
    assert errors(diff_models(reference(a_exe), bazel(b_exe))) == []


def test_config_rules_for_include_paths_ignore_case_on_windows_builds():
    # A tracking log upper-cases paths; one that is not on disk any more (a dead
    # include directory, a moved package) cannot get its case back.
    path_map = [("C:\\VS\\VC", "@vc")]
    a = reference(lib("core", msbuild_compile(
        "src", "a.cpp", "/IC:\\VS\\VC\\UNITTEST\\INCLUDE", "/IX64\\RELEASE\\GENERATED FILES")),
        path_map=path_map)
    b = bazel(lib(":core", bazel_compile("src/a.cpp", "/Ibazel-out/x64/bin/projection")))
    cfg = MigrationConfig(
        ignore_include_prefixes=("@vc/UnitTest/include",),
        include_map=(("src/x64/Release/Generated Files", "@projection"),
                     ("bazel-out/x64/bin/projection", "@projection")))
    assert errors(diff_models(a, b, cfg)) == []
    # Without the rules both paths are real differences.
    (d,) = errors(diff_models(a, b))
    assert d.kind == "includes_diff" and len(d.cmake_only) == 2


def test_a_dll_mapped_to_a_reference_test_is_compared_as_a_test():
    # A CppUnitTest DLL is a plain cc_binary to aquery. Paired with a reference
    # test target by target_map, its sources get the test comparison instead of
    # being reported as an extra product while the tests go unchecked.
    a = reference(lib("Foo.Tests", msbuild_compile("tests", "t.cpp", "/utf-8"),
                      kind=TargetKind.SHARED, role=TargetRole.TEST))
    good = bazel(lib("tests:Foo.Tests.dll", bazel_compile("tests/t.cpp", "/utf-8"),
                     kind=TargetKind.SHARED))
    bad = bazel(lib("tests:Foo.Tests.dll", bazel_compile("tests/t.cpp"), kind=TargetKind.SHARED))
    cfg = MigrationConfig(include_tests=True, align_shared_libs=True,
                          target_map={"Foo.Tests": "tests:Foo.Tests.dll"})
    kinds = {d.kind for d in diff_models(a, good, cfg)}
    assert errors(diff_models(a, good, cfg)) == []
    assert not kinds & {"extra_target", "test_binary_count"}
    (d,) = errors(diff_models(a, bad, cfg))
    assert (d.kind, d.cmake_only) == ("flags_diff", ["/utf-8"])
    # Unmapped, the same Bazel DLL is just an unknown product and the test
    # sources are matched wherever they compile.
    unmapped = MigrationConfig(include_tests=True, align_shared_libs=True)
    assert "test_binary_count" in {d.kind for d in diff_models(a, good, unmapped)}


def test_toolchain_mismatch_blocks_convergence():
    a = reference(lib("core", msbuild_compile("src", "a.cpp")))
    b = bazel(lib(":core", bazel_compile("src/a.cpp")),
              toolchain=dict(TOOLCHAIN, msvc_version="14.51.36231"))
    (d,) = errors(diff_models(a, b))
    assert d.kind == "toolchain_diff" and d.target == "<toolchain>"
    assert d.cmake_only == ["msvc_version=14.44.35207"]
    assert d.bazel_only == ["msvc_version=14.51.36231"]
    # A reviewer may accept it.
    cfg = MigrationConfig(ignore_toolchain={"msvc_version"})
    assert errors(diff_models(a, b, cfg)) == []


def test_unknown_bazel_toolchain_is_a_warning_not_a_pass():
    a = reference(lib("core", msbuild_compile("src", "a.cpp")))
    b = bazel(lib(":core", bazel_compile("src/a.cpp")), toolchain={})
    (d,) = [d for d in diff_models(a, b) if d.kind == "toolchain_diff"]
    assert d.severity == "warn"


def _cs(name, kind, action, role=TargetRole.PRODUCTION):
    return Target(name, kind, actions=[action], role=role, attrs={"language": "c#"})


def test_csharp_is_excluded_until_opted_in():
    msbuild_cs = Action("CSharpCompile", ("csc.exe", "/out:obj\\Tool.dll", "Program.cs",
                                          "obj\\Tool.AssemblyInfo.cs"),
                        cwd=REPO + "\\src\\tool")
    a = reference(lib("core", msbuild_compile("src", "a.cpp")),
                  _cs("Tool", TargetKind.EXECUTABLE, msbuild_cs))
    b = bazel(lib(":core", bazel_compile("src/a.cpp")))
    # A C# executable with no Bazel counterpart is not a finding by default...
    assert errors(diff_models(a, b)) == []
    assert excluded_summary(a, b, MigrationConfig())["cmake"] == {"csharp": ["Tool"]}
    # ...and is one once C# is in scope.
    cfg = MigrationConfig(include_csharp=True)
    (d,) = errors(diff_models(a, b, cfg))
    # One finding for the missing assembly, not one per source it would compile.
    assert (d.kind, d.target) == ("missing_target", "Tool")


def test_csharp_source_sets_compare_across_groupings_and_ignore_generated_files():
    cfg = MigrationConfig(include_csharp=True)
    a = reference(_cs("Tool", TargetKind.STATIC, Action(
        "CSharpCompile", ("csc.exe", "/out:obj\\Tool.dll", "Program.cs", "Util\\Text.cs",
                          "obj\\Release\\Tool.AssemblyInfo.cs"), cwd=REPO + "\\src\\tool")))
    # Bazel passes sources in a param file; they arrive as declared inputs.
    b = bazel(_cs(":tool", TargetKind.STATIC, Action(
        "CSharpCompile", ("dotnet", "csc.dll", "@bazel-out/tool.rsp"),
        inputs=("src/tool/program.cs", "src/tool/Util/Text.cs",
                "bazel-out/x64/bin/src/tool/AssemblyInfo.cs"))))
    assert errors(diff_models(a, b, cfg)) == []
    b.targets[":tool"].actions[0] = Action("CSharpCompile", (), inputs=("src/tool/Program.cs",))
    (d,) = errors(diff_models(a, b, cfg))
    assert (d.kind, d.tu) == ("missing_cs_src", "src/tool/Util/Text.cs")


def test_gnu_dialect_models_are_untouched():
    # A CMake-vs-Bazel(clang) pair must not be routed through the MSVC rules:
    # -MD here is dep-file noise, not a runtime-library choice.
    a = CanonicalModel(build_system=BuildSystem.CMAKE, repo_root="/work/proj")
    a.add(lib("core", Action("CppCompile", ("-std=c++17", "-c", "src/a.cc"))))
    b = CanonicalModel(build_system=BuildSystem.BAZEL, repo_root="/work/proj")
    b.add(lib(":core", Action("CppCompile", ("/usr/bin/clang", "-std=c++17", "-MD", "-MF",
                                             "a.d", "-c", "src/a.cc", "-o", "a.o"))))
    assert diff_models(a, b) == []


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
