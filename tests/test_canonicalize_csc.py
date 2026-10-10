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

"""csc command lines as MSBuild's Csc task and rules_dotnet's CSharpCompile
action really write them (shapes taken from winget-cli and a rules_dotnet
0.22.2 probe)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from canonicalize_csc import (assembly_identity, compiler_index, is_csc,
                              is_framework_pack, package_of, parse, sdk_version,
                              warning_id)

REPO = "C:\\work\\proj"
CWD = REPO + "\\src\\Engine"
NUGET = "C:\\Users\\dev\\.nuget\\packages"
PACKS = "C:\\Program Files\\dotnet\\packs"
MSBUILD_CSC = "C:\\Program Files\\dotnet\\sdk\\10.0.401\\Roslyn\\binfx\\..\\bincore\\csc.exe"
PATH_MAP = [(NUGET + "\\yamldotnet\\16.3.0", "@nuget/yamldotnet"),
            (PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.19", "@dotnet/packs/Microsoft.NETCore.App.Ref")]

MSBUILD = [
    MSBUILD_CSC, "/noconfig", "/unsafe-", "/checked-", "/nowarn:1591,1701,1702", "/fullpaths",
    "/nostdlib+", "/platform:x64", "/errorreport:prompt", "/warn:8",
    "/doc:C:\\work\\proj\\x64\\Release\\Engine\\Engine.xml",
    "/define:TRACE;POWERSHELL_WINDOWS;RELEASE;NET;NET8_0;NETCOREAPP;WINDOWS",
    "/highentropyva+", "/nullable:enable",
    "/reference:" + NUGET + "\\yamldotnet\\16.3.0\\lib\\net8.0\\YamlDotNet.dll",
    "/reference:" + PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.19\\ref\\net8.0\\System.Runtime.dll",
    "/reference:C:\\work\\proj\\src\\Shared\\obj\\Release\\netstandard2.0\\ref\\Shared.dll",
    "/debug-", "/debug:portable", "/filealign:512", "/optimize+",
    "/out:obj\\x64\\Release\\net8.0-windows10.0.26100.0\\Engine.dll",
    "/refout:obj\\x64\\Release\\net8.0-windows10.0.26100.0\\refint\\Engine.dll",
    "/resource:obj\\x64\\Release\\net8.0-windows10.0.26100.0\\Engine.Properties.Resources.resources",
    "/target:library", "/warnaserror+", "/utf8output", "/deterministic+",
    "/sourcelink:obj\\x64\\Release\\Engine.sourcelink.json", "/langversion:12.0",
    "/analyzerconfig:C:\\work\\proj\\.editorconfig",
    "/analyzer:" + NUGET + "\\stylecop.analyzers.unstable\\1.2.0.556\\analyzers\\dotnet\\cs\\StyleCop.Analyzers.dll",
    "/additionalfile:..\\stylecop.json", "/warnaserror+:NU1605,SYSLIB0011",
    "Commands\\Find.cs", "Helpers\\Util.cs",
    "obj\\x64\\Release\\net8.0-windows10.0.26100.0\\Engine.AssemblyInfo.cs",
]

BAZEL = [
    "external/rules_dotnet+/dotnet/private/compiler_wrapper.bat",
    "external/rules_dotnet++dotnet+dotnet_x86_64-pc-windows-msvc/dotnet.exe",
    "external/rules_dotnet++dotnet+dotnet_x86_64-pc-windows-msvc/sdk/10.0.400/Roslyn/bincore/csc.dll",
    "/unsafe-", "/unsafe-", "/checked-", "/nostdlib+", "/utf8output", "/deterministic+",
    "/filealign:512", "/nologo", "/highentropyva+", "/nullable:enable", "/warn:8",
    "/nowarn:CS1701,CS1702", "/nowarn:CS1591", "/target:library", "/langversion:12.0",
    "/debug-", "/optimize+", "/define:TRACE", "/define:RELEASE", "/debug:portable",
    "/out:bazel-out/x64_windows-opt-ST-89742bf2ae83/bin/src/Engine/Engine/net8.0/Engine.dll",
    "/refout:bazel-out/x64_windows-opt-ST-89742bf2ae83/bin/src/Engine/Engine/net8.0/ref/Engine.dll",
    "/doc:bazel-out/x64_windows-opt-ST-89742bf2ae83/bin/src/Engine/Engine/net8.0/Engine.xml",
    "-r:external/+nuget_assets+nuget/yamldotnet/16.3.0/lib/net8.0/YamlDotNet.dll",
    "-r:external/rules_dotnet++targeting_packs_extension+nuget.microsoft.netcore.app.ref.v8.0.30/ref/net8.0/System.Runtime.dll",
    "-r:bazel-out/x64_windows-opt-ST-eb4f073a5b16/bin/src/Shared/Shared/netstandard2.0/ref/Shared.dll",
    "/analyzer:external/+nuget_assets+nuget/stylecop.analyzers.unstable/1.2.0.556/analyzers/dotnet/cs/StyleCop.Analyzers.dll",
    "/additionalfile:src/stylecop.json", "/analyzerconfig:.editorconfig",
    "src/Engine/Commands/Find.cs", "src/Engine/Helpers/Util.cs",
    "/resource:bazel-out/x64/bin/src/Engine/res/Engine.Properties.Resources.resources,Engine.Properties.Resources.resources",
    "/d:NET8_0", "/d:NET", "/d:NETCOREAPP", "/d:WINDOWS", "/d:POWERSHELL_WINDOWS",
    "/warnaserror+", "/warnaserror+:NU1605", "/warnaserror+:SYSLIB0011", "/platform:x64",
]


def test_the_compiler_is_found_behind_launchers():
    assert compiler_index(MSBUILD) == 0 and compiler_index(BAZEL) == 2
    assert is_csc(BAZEL) and not is_csc(["cl.exe", "/c", "a.cpp"])
    assert sdk_version(MSBUILD[0]) == "10.0.401"
    assert sdk_version(BAZEL[2]) == "10.0.400"
    assert sdk_version("C:\\VS\\MSBuild\\Current\\Bin\\Roslyn\\csc.exe") == ""


def test_both_spellings_of_one_compile_are_one_view():
    a = parse(MSBUILD, REPO, cwd=CWD, path_map=PATH_MAP)
    b = parse(BAZEL, REPO)
    assert a.assembly == b.assembly == "Engine"
    assert a.target == b.target == "library"
    # Options: same decisions, however each side spelled or repeated them.
    assert a.options == b.options == (
        "/deterministic", "/doc", "/highentropyva", "/langversion:12.0", "/nostdlib",
        "/nowarn:CS1591", "/nowarn:CS1701", "/nowarn:CS1702", "/nullable:enable",
        "/platform:x64", "/target:library", "/warn:8", "/warnaserror",
        "/warnaserror:NU1605", "/warnaserror:SYSLIB0011")
    assert set(a.defines) <= set(b.defines)
    assert [assembly_identity(r) for r in a.references] == \
        [assembly_identity(r) for r in b.references] == \
        ["yamldotnet.dll", "system.runtime.dll", "shared.dll"]
    assert a.additional_files == b.additional_files == ("src/stylecop.json",)
    assert a.analyzer_configs == b.analyzer_configs == (".editorconfig",)
    assert [name for name, _ in a.resources] == [name for name, _ in b.resources] == \
        ["Engine.Properties.Resources.resources"]


def test_sources_are_repo_relative_and_generated_ones_keep_their_directory():
    a = parse(MSBUILD, REPO, cwd=CWD, path_map=PATH_MAP)
    assert a.sources == (
        "src/Engine/Commands/Find.cs", "src/Engine/Helpers/Util.cs",
        "src/Engine/obj/x64/Release/net8.0-windows10.0.26100.0/Engine.AssemblyInfo.cs")
    assert parse(BAZEL, REPO).sources == ("src/Engine/Commands/Find.cs",
                                          "src/Engine/Helpers/Util.cs")


def test_external_roots_become_tokens_and_package_facts_survive():
    a = parse(MSBUILD, REPO, cwd=CWD, path_map=PATH_MAP)
    assert a.references[0] == "@nuget/yamldotnet/lib/net8.0/YamlDotNet.dll"
    assert package_of(a.raw_references[0]) == ("yamldotnet", "16.3.0")
    assert package_of(a.raw_references[1]) == ("microsoft.netcore.app.ref", "8.0.19")
    assert package_of(a.raw_references[2]) is None           # a project's output
    b = parse(BAZEL, REPO)
    assert package_of(b.raw_references[0]) == ("yamldotnet", "16.3.0")
    assert package_of(b.raw_references[1]) == ("microsoft.netcore.app.ref", "8.0.30")
    assert package_of(b.raw_analyzers[0]) == ("stylecop.analyzers.unstable", "1.2.0.556")
    assert package_of("C:/repo/packages/Microsoft.Windows.CsWinRT.2.3.1/lib/net8.0/WinRT.Runtime.dll") \
        == ("microsoft.windows.cswinrt", "2.3.1")
    assert is_framework_pack("Microsoft.NETCore.App.Ref") and not is_framework_pack("YamlDotNet")


def test_defaults_stated_by_one_side_are_not_flags():
    quiet = parse(["csc.exe", "/target:exe", "a.cs"])
    loud = parse(["csc.exe", "/unsafe-", "/checked-", "/nullable:disable", "/warn:4",
                  "/filealign:512", "/platform:anycpu", "/nowarn:", "/debug+", "/optimize-",
                  "/nologo", "/target:exe", "a.cs"])
    assert quiet.options == loud.options == ("/target:exe",)
    # No /target at all is an exe too.
    assert parse(["csc.exe", "a.cs"]).options == ("/target:exe",)


def test_single_state_options_resolve_last_wins():
    v = parse(["csc.exe", "/langversion:14.0", "/nullable:disable", "/unsafe-", "/warn:3",
               "/langversion:12", "/nullable+", "/unsafe", "/warn:8", "a.cs"])
    assert v.options == ("/langversion:12.0", "/nullable:enable", "/target:exe", "/unsafe",
                         "/warn:8")
    assert parse(["csc.exe", "/unsafe+", "/unsafe-", "a.cs"]).options == ("/target:exe",)


def test_warning_lists_split_and_normalize():
    assert warning_id("1591") == "CS1591" and warning_id("cs0168") == "CS0168"
    assert warning_id("CA1416") == "CA1416" and warning_id("NU1605") == "NU1605"
    v = parse(["csc.exe", "/nowarn:1591;CS0168,CA1416", "/warnaserror+:CS0168",
               "/warnaserror-:CS0618,CA1416", "a.cs"])
    assert v.options == ("/nowarn:CA1416", "/nowarn:CS0168", "/nowarn:CS1591", "/target:exe",
                         "/warnaserror-:CA1416", "/warnaserror-:CS0618", "/warnaserror:CS0168")
    # Turning everything into errors and then exempting ids keeps both facts.
    v = parse(["csc.exe", "/warnaserror+", "/warnaserror-:CS0618", "a.cs"])
    assert v.options == ("/target:exe", "/warnaserror", "/warnaserror-:CS0618")
    assert parse(["csc.exe", "/warnaserror+", "/warnaserror-", "a.cs"]).options == ("/target:exe",)


def test_reference_aliases_and_lists():
    v = parse(["csc.exe", "/reference:Alias=C:\\libs\\A.dll", "/r:C:\\libs\\B.dll;C:\\libs\\C.dll",
               "/link:C:\\libs\\Interop.dll", "a.cs"])
    assert [assembly_identity(r) for r in v.references] == ["a.dll", "b.dll", "c.dll", "interop.dll"]


def test_resources_default_to_the_file_name():
    v = parse(["csc.exe", "/resource:obj\\Foo.Strings.resources",
               "/resource:res\\logo.png,Foo.Assets.logo.png,public", "a.cs"], REPO, cwd=CWD)
    assert v.resources == (("Foo.Assets.logo.png", "src/Engine/res/logo.png"),
                           ("Foo.Strings.resources", "src/Engine/obj/Foo.Strings.resources"))


def test_unknown_options_are_kept_and_features_split():
    v = parse(["csc.exe", "/features:strict;InterceptorsNamespaces=Foo", "/brandnew:1",
               "/keyfile:..\\key.snk", "/publicsign+", "a.cs"], REPO, cwd=CWD)
    assert v.options == ("/brandnew:1", "/features:InterceptorsNamespaces=Foo", "/features:strict",
                         "/keyfile:src/key.snk", "/publicsign", "/target:exe")


def test_ref_only_twin_is_marked():
    v = parse(BAZEL[:3] + ["/refonly", "/out:bazel-out/x/bin/a/ref/A.dll", "a.cs"])
    assert v.ref_only and v.assembly == "A"


def test_posix_paths_are_sources_not_options():
    v = parse(["csc", "/target:library", "/work/proj/src/a.cs", "-r:/usr/lib/dotnet/x.dll"],
              "/work/proj")
    assert v.sources == ("src/a.cs",) and assembly_identity(v.references[0]) == "x.dll"


def test_an_analyzer_diagnostic_id_is_matched_as_written():
    # Compiler ids are case-insensitive and may omit the prefix; an analyzer's
    # id is not `CS<number>` and Roslyn compares it exactly.
    import canonicalize_csc as csc
    assert [csc.warning_id(x) for x in ("1591", "cs1591", "CS1591", "CS8002")] == [
        "CS1591", "CS1591", "CS1591", "CS8002"]
    assert csc.warning_id("CsWinRT1028") == "CsWinRT1028"
    view = csc.parse(["csc.exe", "/warnaserror-:CsWinRT1028;ca1416", "/nowarn:1701", "A.cs"])
    assert "/warnaserror-:CsWinRT1028" in view.options and "/warnaserror-:ca1416" in view.options
    assert "/nowarn:CS1701" in view.options


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
