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

"""The differ on C#: an MSBuild reference model (Csc task command lines)
against a Bazel model built with rules_dotnet (CSharpCompile actions)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from config import MigrationConfig
from diff import diff_models
from model import Action, BuildSystem, CanonicalModel, Target, TargetKind, TargetRole

REPO = "C:\\work\\proj"
NUGET = "C:\\Users\\dev\\.nuget\\packages"
PACKS = "C:\\Program Files\\dotnet\\packs"
CSC = "C:\\Program Files\\dotnet\\sdk\\10.0.401\\Roslyn\\bincore\\csc.exe"
BAZEL_CSC = ["external/rules_dotnet+/dotnet/private/compiler_wrapper.bat",
             "external/rules_dotnet++dotnet+dotnet_x86_64-pc-windows-msvc/dotnet.exe",
             "external/rules_dotnet++dotnet+dotnet_x86_64-pc-windows-msvc/sdk/10.0.401/Roslyn/bincore/csc.dll"]
# What rules_dotnet puts on every compile.
BAZEL_DEFAULTS = ["/unsafe-", "/checked-", "/nostdlib+", "/utf8output", "/deterministic+",
                  "/filealign:512", "/nologo", "/highentropyva+", "/nowarn:CS1701,CS1702",
                  "/debug-", "/optimize+", "/define:TRACE", "/define:RELEASE", "/debug:portable"]
CFG = MigrationConfig(include_csharp=True)


def msbuild_compile(project, tfm, *extra, sources=("A.cs",), refs=(), target="library"):
    cwd = REPO + "\\src\\" + project
    args = [CSC, "/noconfig", "/nostdlib+", "/deterministic+", "/highentropyva+",
            "/nowarn:1701,1702", "/define:TRACE;RELEASE;NET;NET8_0", "/langversion:12.0",
            "/target:" + target, "/out:obj\\Release\\%s\\%s.dll" % (tfm, project),
            "/reference:" + PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.31\\ref\\net8.0\\System.Runtime.dll",
            *["/reference:" + r for r in refs], *extra, *sources,
            "obj\\Release\\%s\\%s.AssemblyInfo.cs" % (tfm, project)]
    return Action("CSharpCompile", tuple(args), cwd=cwd)


def bazel_compile(project, tfm, *extra, sources=None, refs=(), target="library", pack="8.0.31"):
    sources = sources if sources is not None else ("src/%s/A.cs" % project,)
    out = "bazel-out/x64_windows-opt-ST-0123456789ab/bin/src/%s/%s/%s/%s.dll" % (
        project, project, tfm, project)
    args = [*BAZEL_CSC, *BAZEL_DEFAULTS, "/langversion:12.0", "/target:" + target, "/out:" + out,
            "-r:external/rules_dotnet++targeting_packs_extension+nuget.microsoft.netcore.app.ref.v%s"
            "/ref/net8.0/System.Runtime.dll" % pack,
            *["-r:" + r for r in refs], *extra, *sources, "/d:NET8_0", "/d:NET"]
    return Action("CSharpCompile", tuple(args))


def model(build_system, *targets, toolchain=None):
    m = CanonicalModel(build_system=build_system, repo_root=REPO, toolchain=dict(toolchain or {}))
    for t in targets:
        m.add(t)
    return m


def cs(name, action, kind=TargetKind.STATIC, role=TargetRole.PRODUCTION):
    return Target(name, kind, actions=[action], role=role, attrs={"language": "c#"})


def reference(*targets, **kw):
    return model(BuildSystem.MSBUILD, *targets, **kw)


def bazel(*targets, **kw):
    return model(BuildSystem.BAZEL, *targets, **kw)


def errors(discs):
    return [d for d in discs if d.severity == "error"]


def test_the_same_assembly_converges_whatever_the_targets_are_called():
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0-windows10.0.26100.0")))
    b = bazel(cs("src/Engine:Engine", bazel_compile("Engine", "net8.0")))
    assert errors(diff_models(a, b, CFG)) == []
    # Nothing is compared until C# is opted in.
    broken = bazel(cs("src/Engine:Engine", bazel_compile("Engine", "net8.0", "/langversion:14.0")))
    assert errors(diff_models(a, broken)) == []
    assert errors(diff_models(a, broken, CFG)) != []


def test_a_multi_targeted_project_is_one_assembly_per_framework():
    a = reference(cs("Engine[net48]", msbuild_compile("Engine", "net48")),
                  cs("Engine[net8.0-windows10.0.26100.0]",
                     msbuild_compile("Engine", "net8.0-windows10.0.26100.0")))
    only_one = bazel(cs("src/Engine:Engine", bazel_compile("Engine", "net8.0")))
    (d,) = errors(diff_models(a, only_one, CFG))
    assert (d.kind, d.target) == ("missing_target", "Engine [net48]")
    both = bazel(cs("src/Engine:Engine", bazel_compile("Engine", "net8.0")),
                 cs("src/Engine:Engine_net48", bazel_compile("Engine", "net48")))
    assert errors(diff_models(a, both, CFG)) == []


def test_options_and_defines_are_compared():
    a = reference(cs("Engine", msbuild_compile(
        "Engine", "net8.0", "/nullable:enable", "/unsafe+", "/platform:x64", "/warnaserror+",
        "/define:POWERSHELL_WINDOWS")))
    same = bazel(cs(":e", bazel_compile(
        "Engine", "net8.0", "/nullable:enable", "/unsafe+", "/platform:x64", "/warnaserror+",
        "/d:POWERSHELL_WINDOWS")))
    assert errors(diff_models(a, same, CFG)) == []
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", "/nullable:disable", "/warnaserror+")))
    found = {d.kind: d for d in errors(diff_models(a, b, CFG))}
    assert found["flags_diff"].cmake_only == ["/nullable:enable", "/platform:x64", "/unsafe"]
    assert found["defines_diff"].cmake_only == ["POWERSHELL_WINDOWS"]
    # The language version each toolchain defaults to is different; it must be stated.
    c = bazel(cs(":e", Action("CSharpCompile", tuple(
        x for x in same.targets[":e"].actions[0].arguments if x != "/langversion:12.0")
        + ("/langversion:14.0",))))
    (d,) = errors(diff_models(a, c, CFG))
    assert (d.kind, d.cmake_only) == ("flags_diff", ["/langversion:12.0"])


def test_a_default_changed_on_the_bazel_side_is_not_an_extra_flag():
    # The reference says nothing about nullable, unsafe or the platform: it has
    # the compiler's defaults. Bazel setting them is a different assembly.
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0")))
    for option, canonical in (("/nullable:enable", "/nullable:enable"), ("/unsafe+", "/unsafe"),
                              ("/checked+", "/checked"), ("/platform:x64", "/platform:x64")):
        b = bazel(cs(":e", bazel_compile("Engine", "net8.0", option)))
        (d,) = errors(diff_models(a, b, CFG))
        assert (d.kind, d.cmake_only, d.bazel_only) == ("flags_diff", [], [canonical])
    # An option with no default to depart from stays a tolerated extra.
    extra = bazel(cs(":e", bazel_compile("Engine", "net8.0", "/features:strict")))
    assert errors(diff_models(a, extra, CFG)) == []
    # And a reviewer can accept one.
    accepted = MigrationConfig(include_csharp=True, ignore_flags={"/platform:x64"})
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", "/platform:x64")))
    assert errors(diff_models(a, b, accepted)) == []


def test_missing_reference_and_wrong_package_version_are_caught():
    yaml = NUGET + "\\yamldotnet\\16.3.0\\lib\\net8.0\\YamlDotNet.dll"
    shared = REPO + "\\src\\Shared\\obj\\Release\\netstandard2.0\\ref\\Shared.dll"
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0", refs=(yaml, shared))))
    ok = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(
        "external/+nuget_assets+nuget/yamldotnet/16.3.0/lib/net8.0/YamlDotNet.dll",
        "bazel-out/x64/bin/src/Shared/Shared/netstandard2.0/ref/Shared.dll"))))
    assert errors(diff_models(a, ok, CFG)) == []
    dropped = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(
        "bazel-out/x64/bin/src/Shared/Shared/netstandard2.0/ref/Shared.dll",))))
    (d,) = errors(diff_models(a, dropped, CFG))
    assert (d.kind, d.cmake_only) == ("cs_refs_diff", ["yamldotnet.dll (yamldotnet 16.3.0)"])
    older = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(
        "external/+nuget_assets+nuget/yamldotnet/15.0.0/lib/net8.0/YamlDotNet.dll",
        "bazel-out/x64/bin/src/Shared/Shared/netstandard2.0/ref/Shared.dll"))))
    (d,) = errors(diff_models(a, older, CFG))
    assert d.kind == "cs_refs_diff"
    assert d.cmake_only == ["yamldotnet.dll: reference yamldotnet 16.3.0, bazel yamldotnet 15.0.0"]


def test_a_reference_only_bazel_passes_is_reported():
    # An extra reference is not inert: a second copy of a type makes calls
    # ambiguous, a namespace it declares can shadow the one the source means.
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0")))
    pack = ("external/rules_dotnet++targeting_packs_extension+nuget.microsoft.netcore.app.ref"
            ".v8.0.31/ref/net8.0/")
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(
        "external/+nuget_assets+nuget/microsoft.windows.cswinrt/2.3.1/lib/net8.0/WinRT.Runtime.dll",
        pack + "System.Linq.dll", pack + "System.Net.Http.dll"))))
    found = diff_models(a, b, CFG)
    assert errors(found) == []
    (w,) = [d for d in found if d.kind == "cs_refs_diff"]
    assert w.severity == "warn"
    # A rule set that passes a whole reference pack is one line, not hundreds.
    assert w.bazel_only == ["winrt.runtime.dll",
                            "2 more assemblies of microsoft.netcore.app.ref 8.0.31"]


def test_framework_pack_version_is_one_toolchain_fact_not_a_reference_diff():
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0")),
                  toolchain={"dotnet_sdk_version": "10.0.401",
                             "pack:microsoft.netcore.app.ref": "8.0.31"})
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", pack="8.0.30")),
              toolchain={"dotnet_sdk_version": "10.0.400",
                         "pack:microsoft.netcore.app.ref": "8.0.30"})
    found = errors(diff_models(a, b, CFG))
    assert sorted((d.kind, d.cmake_only[0]) for d in found) == [
        ("toolchain_diff", "dotnet_sdk_version=10.0.401"),
        ("toolchain_diff", "pack:microsoft.netcore.app.ref=8.0.31")]
    accepted = MigrationConfig(include_csharp=True, ignore_toolchain={
        "dotnet_sdk_version", "pack:microsoft.netcore.app.ref"})
    assert errors(diff_models(a, b, accepted)) == []


def test_analyzers_resources_and_compile_inputs_are_compared():
    stylecop = NUGET + "\\stylecop.analyzers.unstable\\1.2.0.556\\analyzers\\dotnet\\cs\\StyleCop.Analyzers.dll"
    a = reference(cs("Engine", msbuild_compile(
        "Engine", "net8.0", "/analyzer:" + stylecop, "/additionalfile:..\\stylecop.json",
        "/analyzerconfig:" + REPO + "\\.editorconfig",
        "/analyzerconfig:obj\\Release\\net8.0\\Engine.GeneratedMSBuildEditorConfig.editorconfig",
        "/resource:obj\\Release\\net8.0\\Engine.Properties.Resources.resources")))
    ok = bazel(cs(":e", bazel_compile(
        "Engine", "net8.0",
        "/analyzer:external/+nuget_assets+nuget/stylecop.analyzers.unstable/1.2.0.556/analyzers/dotnet/cs/StyleCop.Analyzers.dll",
        "/additionalfile:src/stylecop.json", "/analyzerconfig:.editorconfig",
        "/analyzerconfig:bazel-out/x64/bin/src/Engine/Engine.msbuild.editorconfig",
        "/resource:bazel-out/x64/bin/src/Engine/res.resources,Engine.Properties.Resources.resources")))
    assert errors(diff_models(a, ok, CFG)) == []
    bare = bazel(cs(":e", bazel_compile("Engine", "net8.0")))
    found = {d.kind: d for d in errors(diff_models(a, bare, CFG))}
    assert found["cs_analyzers_diff"].cmake_only == [
        "stylecop.analyzers.dll (stylecop.analyzers.unstable 1.2.0.556)"]
    assert found["cs_resources_diff"].cmake_only == [
        "additionalfile:src/stylecop.json", "analyzerconfig:.editorconfig",
        "analyzerconfig:<generated>.editorconfig",
        "resource:Engine.Properties.Resources.resources"]


def test_sources_are_compared_per_assembly_and_generated_trees_need_a_mapping():
    projection = ["obj\\Release\\net8.0\\Generated Files\\CsWinRT\\Foo.cs",
                  "obj\\Release\\net8.0\\Generated Files\\CsWinRT\\Bar.cs"]
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0", sources=("A.cs", "B.cs", *projection))))
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", sources=(
        "src/Engine/A.cs", "bazel-out/x64/bin/src/Engine/projection/Foo.cs"))))
    # Unmapped, each build's generated files are its own business -- but the
    # report says that they were not compared.
    found = diff_models(a, b, CFG)
    (d,) = errors(found)
    assert (d.kind, d.tu) == ("missing_cs_src", "src/Engine/B.cs")
    (w,) = [d for d in found if d.severity == "warn" and d.kind == "missing_cs_src"]
    assert w.cmake_only == ["src/Engine/obj/Release/net8.0/Engine.AssemblyInfo.cs",
                            "src/Engine/obj/Release/net8.0/Generated Files/CsWinRT/Bar.cs",
                            "src/Engine/obj/Release/net8.0/Generated Files/CsWinRT/Foo.cs"]
    # Rules name a bazel output the way the `bazel-bin` link does: the
    # configuration directory under `bazel-out` is not stable.
    mapped = MigrationConfig(include_csharp=True, source_dir_map=(
        ("src/Engine/obj/Release/net8.0/Generated Files/CsWinRT/", "@projection/"),
        ("bazel-bin/src/Engine/projection/", "@projection/")))
    found = sorted((d.kind, d.tu) for d in errors(diff_models(a, b, mapped)))
    assert found == [("missing_cs_src", "src/Engine/B.cs"),
                     ("missing_cs_src", "src/Engine/obj/Release/net8.0/Generated Files/CsWinRT/Bar.cs")]
    # A tree artifact on the Bazel side is one argument: collapse the tree instead.
    collapsed = MigrationConfig(include_csharp=True, source_map=(
        ("src/Engine/obj/Release/net8.0/Generated Files/CsWinRT/", "@projection"),
        ("bazel-bin/src/Engine/projection.cs", "@projection")))
    tree = bazel(cs(":e", bazel_compile("Engine", "net8.0", sources=(
        "src/Engine/A.cs", "src/Engine/B.cs", "bazel-out/x64/bin/src/Engine/projection.cs"))))
    assert errors(diff_models(a, tree, collapsed)) == []


def test_a_test_assembly_is_compared_only_with_tests_in_scope():
    a = reference(cs("Engine.Tests", msbuild_compile("Engine.Tests", "net8.0", "/unsafe+", target="exe"),
                     kind=TargetKind.EXECUTABLE, role=TargetRole.TEST))
    b = bazel(cs(":t", bazel_compile("Engine.Tests", "net8.0", target="exe"), kind=TargetKind.EXECUTABLE))
    assert errors(diff_models(a, b, CFG)) == []
    with_tests = MigrationConfig(include_csharp=True, include_tests=True)
    (d,) = errors(diff_models(a, b, with_tests))
    assert (d.kind, d.target, d.cmake_only) == ("flags_diff", "Engine.Tests [net8.0]", ["/unsafe"])


def test_a_compile_without_a_command_line_is_called_out():
    skipped = Action("CSharpCompile", (), inputs=(REPO + "\\src\\Engine\\A.cs",),
                     outputs=(REPO + "\\src\\Engine\\obj\\Release\\net8.0\\Engine.dll",),
                     cwd=REPO + "\\src\\Engine")
    a = reference(cs("Engine", skipped))
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", "/unsafe+")))
    discs = diff_models(a, b, CFG)
    assert errors(discs) == []
    (w,) = [d for d in discs if d.kind == "flags_diff"]
    assert w.severity == "warn" and "no compiler command line" in w.detail


def test_one_assembly_name_from_two_files_is_an_error():
    # rules_dotnet passes its whole base pack, facade WindowsBase.dll included,
    # next to the real one from WindowsDesktop; MSBuild passes only the latter.
    desktop = "\\Microsoft.WindowsDesktop.App.Ref\\8.0.31\\ref\\net8.0\\WindowsBase.dll"
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0", refs=(PACKS + desktop,))))
    base = ("external/rules_dotnet++targeting_packs_extension+nuget.microsoft.netcore.app.ref"
            ".v8.0.31/ref/net8.0/WindowsBase.dll")
    locked = "external/+nuget_assets+nuget/microsoft.windowsdesktop.app.ref/8.0.31/ref/net8.0/WindowsBase.dll"
    both = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(base, locked))))
    (d,) = errors(diff_models(a, both, CFG))
    assert d.kind == "cs_refs_diff" and "two different files for one assembly name" in d.detail
    assert d.bazel_only[0] == "windowsbase.dll" and len(d.bazel_only) == 3
    one = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(locked,))))
    assert errors(diff_models(a, one, CFG)) == []


def test_an_option_naming_a_file_is_compared_by_the_file():
    # The reference names a manifest MSBuild generated into obj/; bazel names
    # the restated copy in its output tree. Same file, two places.
    a = reference(cs("App", msbuild_compile(
        "App", "net8.0", "/win32manifest:obj\\Release\\net8.0\\Manifests\\app.manifest", target="winexe"),
        kind=TargetKind.EXECUTABLE))
    b = bazel(cs(":a", bazel_compile(
        "App", "net8.0", "/target:winexe",
        "/win32manifest:bazel-out/x64/bin/src/App/_msbuild/App/app.manifest", target="exe"),
        kind=TargetKind.EXECUTABLE))
    assert errors(diff_models(a, b, CFG)) == []
    # A hand-written one is a repo path, and a different one is a difference.
    a = reference(cs("App", msbuild_compile("App", "net8.0", "/win32manifest:app.manifest", target="winexe"),
                     kind=TargetKind.EXECUTABLE))
    b = bazel(cs(":a", bazel_compile("App", "net8.0", "/target:winexe",
                                     "/win32manifest:src/App/other.manifest", target="exe"),
                 kind=TargetKind.EXECUTABLE))
    (d,) = errors(diff_models(a, b, CFG))
    assert d.cmake_only == ["/win32manifest:src/app/app.manifest"]      # identity: case folded
    # A console program where the reference builds one for the Windows
    # subsystem: rules_dotnet's `winexe` attribute alone gives exactly this.
    console = bazel(cs(":a", bazel_compile("App", "net8.0", "/win32manifest:src/App/app.manifest",
                                           target="exe"), kind=TargetKind.EXECUTABLE))
    (d,) = errors(diff_models(a, console, CFG))
    assert d.cmake_only == ["/target:winexe"] and d.bazel_only == ["/target:exe"]


def test_generated_sources_pair_whatever_the_case_of_the_directory():
    # MSBuild spells a directory as the project file does, Bazel as the file
    # system does.
    a = reference(cs("Common", msbuild_compile("Common", "net8.0")))
    b = bazel(cs(":c", bazel_compile("Common", "net8.0", sources=(
        "src/Common/A.cs", "bazel-out/x64/bin/src/common/_msbuild/Common/Common.AssemblyInfo.cs"))))
    cfg = MigrationConfig(include_csharp=True, source_map=(
        ("src/Common/obj/Release/net8.0/Common.AssemblyInfo.cs", "@msbuild/Common/assembly_info"),
        ("bazel-bin/src/Common/_msbuild/Common/Common.AssemblyInfo.cs", "@msbuild/Common/assembly_info")))
    found = diff_models(a, b, cfg)
    assert errors(found) == []
    assert not [d for d in found if d.kind in ("extra_cs_src", "missing_cs_src")]


def test_an_assembly_taken_from_another_framework_pack_is_an_error():
    # Same file name, and each build passes one file: but the base pack's
    # WindowsBase.dll is a facade and WindowsDesktop's is the real thing.
    desktop = PACKS + "\\Microsoft.WindowsDesktop.App.Ref\\8.0.31\\ref\\net8.0\\WindowsBase.dll"
    a = reference(cs("Engine", msbuild_compile("Engine", "net8.0", refs=(desktop,))))
    facade = ("external/rules_dotnet++targeting_packs_extension+nuget.microsoft.netcore.app.ref"
              ".v8.0.31/ref/net8.0/WindowsBase.dll")
    b = bazel(cs(":e", bazel_compile("Engine", "net8.0", refs=(facade,))))
    (d,) = errors(diff_models(a, b, CFG))
    assert d.kind == "cs_refs_diff"
    assert d.cmake_only == ["windowsbase.dll: reference takes it from microsoft.windowsdesktop.app.ref, "
                            "bazel from microsoft.netcore.app.ref"]
    # The installed .NET Framework targeting pack and the package that copies
    # it are the same pack; so are two versions of one pack (a toolchain fact).
    netfx = "C:\\Program Files (x86)\\Reference Assemblies\\Microsoft\\Framework\\.NETFramework\\v4.8\\System.Core.dll"
    a = reference(cs("Engine", msbuild_compile("Engine", "net48", refs=(netfx,))))
    b = bazel(cs(":e", bazel_compile("Engine", "net48", refs=(
        "external/+nuget_assets+nuget/microsoft.netframework.referenceassemblies.net48/1.0.3"
        "/build/.NETFramework/v4.8/System.Core.dll",))))
    assert [d for d in errors(diff_models(a, b, CFG)) if d.kind == "cs_refs_diff"] == []


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
