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

"""The C# digest of the MSBuild report and the NuGet lock derived from it."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import msbuild_csharp as csharp
import nuget_assets

REPO = "C:\\work\\proj"
CWD = REPO + "\\src\\Engine"
NUGET = "C:\\Users\\dev\\.nuget\\packages"
PACKS = "C:\\Program Files\\dotnet\\packs"
SDK = "C:\\Program Files\\dotnet\\sdk\\10.0.401"
OBJ = "obj\\Release\\net8.0-windows10.0.26100.0"

ARGS = [
    SDK + "\\Roslyn\\bincore\\csc.exe", "/noconfig", "/nullable:enable", "/define:TRACE;NET8_0;WINDOWS",
    "/reference:" + PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.31\\ref\\net8.0\\System.Runtime.dll",
    "/reference:" + PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.31\\ref\\net8.0\\System.Linq.dll",
    "/reference:" + NUGET + "\\yamldotnet\\16.3.0\\lib\\net8.0\\YamlDotNet.dll",
    "/reference:" + REPO + "\\src\\Shared\\obj\\Release\\netstandard2.0\\ref\\Shared.dll",
    "/reference:" + REPO + "\\x64\\Release\\Native\\Native.winmd",
    "/analyzer:" + NUGET + "\\stylecop.analyzers\\1.1.118\\analyzers\\dotnet\\cs\\StyleCop.Analyzers.dll",
    "/analyzer:" + SDK + "\\Sdks\\Microsoft.NET.Sdk\\analyzers\\Microsoft.CodeAnalysis.NetAnalyzers.dll",
    "/analyzer:" + PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.31\\analyzers\\dotnet\\cs\\System.Text.Json.SourceGeneration.dll",
    "/additionalfile:..\\stylecop.json",
    "/analyzerconfig:" + REPO + "\\.editorconfig",
    "/analyzerconfig:" + OBJ + "\\Engine.GeneratedMSBuildEditorConfig.editorconfig",
    "/analyzerconfig:" + SDK + "\\Sdks\\Microsoft.NET.Sdk\\analyzers\\build\\config\\analysislevel_8_default.globalconfig",
    "/resource:" + OBJ + "\\Engine.Properties.Resources.resources",
    "/target:library", "/langversion:12.0", "/out:" + OBJ + "\\Engine.dll",
    "Commands\\Find.cs", "..\\Common\\Shared.cs",
    OBJ + "\\Engine.AssemblyInfo.cs",
    OBJ + "\\.NETCoreApp,Version=v8.0.AssemblyAttributes.cs",
    OBJ + "\\Generated Files\\CsWinRT\\Foo.cs", OBJ + "\\Generated Files\\CsWinRT\\Bar.cs",
    NUGET + "\\microsoft.net.test.sdk\\17.14.1\\build\\net8.0\\Microsoft.NET.Test.Sdk.Program.cs",
]


def _digest(**kw):
    generated = lambda p: "\\obj\\" in p.lower() or p.lower().endswith(".winmd")
    owner = lambda p: "Shared" if p.lower().endswith("\\shared.dll") else None
    return csharp.digest(ARGS, REPO, CWD, is_generated=generated, project_of=owner, **kw)


def test_paths_outside_the_repo_are_classified_by_what_they_are():
    f = csharp.classify_external
    assert f(NUGET + "\\yamldotnet\\16.3.0\\lib\\net8.0\\YamlDotNet.dll") == {
        "kind": "nuget", "id": "yamldotnet", "version": "16.3.0", "path": "lib/net8.0/YamlDotNet.dll"}
    # A tool at the root of a package, not under any asset directory.
    assert f(NUGET + "\\microsoft.windows.cswinrt\\2.3.1\\cswinrt.exe") == {
        "kind": "nuget", "id": "microsoft.windows.cswinrt", "version": "2.3.1", "path": "cswinrt.exe"}
    assert f(PACKS + "\\Microsoft.NETCore.App.Ref\\8.0.31\\ref\\net8.0\\System.Runtime.dll")["kind"] == "pack"
    assert f(SDK + "\\Sdks\\Microsoft.NET.Sdk\\analyzers\\X.dll") == {
        "kind": "sdk", "version": "10.0.401", "path": "Sdks/Microsoft.NET.Sdk/analyzers/X.dll"}
    netfx = f("C:\\Program Files (x86)\\Reference Assemblies\\Microsoft\\Framework\\.NETFramework\\v4.8\\mscorlib.dll")
    assert (netfx["kind"], netfx["version"], netfx["path"]) == ("pack", "v4.8", "mscorlib.dll")
    assert f("C:\\Tools\\loose.dll") is None


def test_sources_are_split_by_who_wrote_them():
    s = _digest()["sources"]
    assert s["hand_written"] == ["src/Common/Shared.cs", "src/Engine/Commands/Find.cs"]
    kinds = {g["kind"]: g for g in s["generated"]}
    assert set(kinds) == {"assembly_info", "target_framework_attribute", "cswinrt_projection"}
    # A projection is hundreds of files: reported as its directory.
    assert kinds["cswinrt_projection"] == {
        "kind": "cswinrt_projection", "files": 2,
        "dir": "src/Engine/obj/Release/net8.0-windows10.0.26100.0/Generated Files/CsWinRT"}
    assert s["external"] == [{"kind": "nuget", "id": "microsoft.net.test.sdk", "version": "17.14.1",
                              "path": "build/net8.0/Microsoft.NET.Test.Sdk.Program.cs"}]


def test_references_and_analyzers_are_grouped_by_origin():
    d = _digest()
    assert d["assembly"] == "Engine" and d["target"] == "library" and d["compiler_sdk"] == "10.0.401"
    r = d["references"]
    # A framework pack is 160 assemblies; its identity and size are what matter.
    assert r["packs"] == {"microsoft.netcore.app.ref/8.0.31": 2}
    assert r["nuget"] == [{"id": "yamldotnet", "version": "16.3.0", "path": "lib/net8.0/YamlDotNet.dll"}]
    assert r["projects"] == [{"target": "Shared",
                              "path": "src/Shared/obj/Release/netstandard2.0/ref/Shared.dll"}]
    assert r["other"] == ["x64/Release/Native/Native.winmd"]
    a = d["analyzers"]
    assert a["sdk"] == ["Sdks/Microsoft.NET.Sdk/analyzers/Microsoft.CodeAnalysis.NetAnalyzers.dll"]
    assert a["packs"] == {"microsoft.netcore.app.ref/8.0.31": 1}
    assert [x["id"] for x in a["nuget"]] == ["stylecop.analyzers"]


def test_compile_inputs_and_resources_keep_their_origin():
    resx = {(CWD + "\\" + OBJ + "\\Engine.Properties.Resources.resources").lower():
            "src/Engine/Properties/Resources.resx"}
    d = _digest(resource_sources=resx)
    assert d["additional_files"] == [{"kind": "repo", "path": "src/stylecop.json"}]
    assert [(c["kind"], c["path"].rsplit("/", 1)[-1]) for c in d["analyzer_configs"]] == [
        ("repo", ".editorconfig"), ("generated", "Engine.GeneratedMSBuildEditorConfig.editorconfig"),
        ("sdk", "analysislevel_8_default.globalconfig")]
    (resource,) = d["resources"]
    assert resource["name"] == "Engine.Properties.Resources.resources"
    assert resource["from"] == "src/Engine/Properties/Resources.resx"


# ---- the NuGet lock ----------------------------------------------------------

def _report(digest, steps=(), target="exe"):
    digest = dict(digest, target_framework="net8.0-windows10.0.26100.0", target=target,
                  assets_file="src/Engine/obj/project.assets.json")
    return {"targets": {"Engine": {"project": "src/Engine/Engine.csproj", "csharp": digest,
                                   "steps": list(steps)}}}


ASSETS = {
    "packageFolders": {},
    "libraries": {
        "YamlDotNet/16.3.0": {"type": "package", "sha512": "CONTENTHASH=="},
        "Microsoft.Win32.Registry/5.0.0": {"type": "package", "sha512": "REG=="},
        "Native.Runtime/1.0.0": {"type": "package", "sha512": "NAT=="},
    },
    "targets": {
        "net8.0-windows10.0.26100.0": {
            "YamlDotNet/16.3.0": {"type": "package", "compile": {"lib/net8.0/YamlDotNet.dll": {}},
                                  "runtime": {"lib/net8.0/YamlDotNet.dll": {}}},
            # In the framework already: MSBuild's conflict resolution dropped it.
            "Microsoft.Win32.Registry/5.0.0": {
                "type": "package", "compile": {"ref/netstandard2.0/Microsoft.Win32.Registry.dll": {}},
                "runtime": {"lib/netstandard2.0/Microsoft.Win32.Registry.dll": {}}},
            # Nothing to compile against, something to load.
            "Native.Runtime/1.0.0": {"type": "package", "compile": {"lib/net8.0/_._": {}},
                                     "runtime": {"lib/net8.0/Native.Runtime.dll": {}},
                                     "native": {"runtimes/win-x64/native/native.dll": {}}},
            "Shared/1.0.0": {"type": "project"},
        },
    },
    "project": {"restore": {"sources": {"C:\\Program Files\\dotnet\\library-packs": {},
                                        "https://feed.example/v3/index.json": {}}},
                "frameworks": {"net8.0-windows10.0.26100.0": {"targetAlias": "net8.0-windows10.0.26100.0"}}},
}


def test_lock_records_what_was_compiled_and_what_is_needed_at_run_time():
    steps = [{"kind": "exec", "command": '"%s\\microsoft.windows.cswinrt\\2.3.1\\cswinrt.exe" @"x.rsp"' % NUGET},
             {"kind": "copy", "from": [(NUGET + "\\microsoft.windows.cswinrt\\2.3.1\\embedded\\any\\A.cs").replace("\\", "/")],
              "to": "src/Engine/obj"}]
    resx = {(CWD + "\\" + OBJ + "\\Engine.Properties.Resources.resources").lower():
            "src/Engine/Properties/Resources.resx"}
    lock = nuget_assets.build_lock(_report(_digest(resource_sources=resx), steps), REPO,
                                   read_assets=lambda p: ASSETS)
    assert lock["sources"] == ["https://feed.example/v3/index.json"]     # not the local folder
    target = lock["targets"]["Engine"]
    assert target["framework"] == "net8.0-windows10.0.26100.0"
    assert target["packages"] == {key: "net8.0-windows10.0.26100.0" for key in (
        "native.runtime/1.0.0", "stylecop.analyzers/1.1.118", "yamldotnet/16.3.0")}
    assert target["displaced"] == ["microsoft.win32.registry/5.0.0"]
    yaml = lock["packages"]["yamldotnet/16.3.0"]
    assert yaml["id"] == "YamlDotNet"
    assert yaml["frameworks"]["net8.0-windows10.0.26100.0"] == {
        "compile": ["lib/net8.0/YamlDotNet.dll"], "runtime": ["lib/net8.0/YamlDotNet.dll"]}
    assert lock["packages"]["native.runtime/1.0.0"]["frameworks"]["net8.0-windows10.0.26100.0"] == {
        "native": ["runtimes/win-x64/native/native.dll"], "runtime": ["lib/net8.0/Native.Runtime.dll"]}
    assert lock["packages"]["stylecop.analyzers/1.1.118"]["frameworks"][
        "net8.0-windows10.0.26100.0"] == {"analyzers": ["analyzers/dotnet/cs/StyleCop.Analyzers.dll"]}
    # Files a compile or a build step names directly are exported, not selected.
    assert target["files"] == {
        "microsoft.net.test.sdk/17.14.1": ["build/net8.0/Microsoft.NET.Test.Sdk.Program.cs"],
        "microsoft.windows.cswinrt/2.3.1": ["cswinrt.exe", "embedded/any/A.cs"]}
    assert "microsoft.win32.registry/5.0.0" not in lock["packages"]


def test_integrity_is_the_hash_of_the_package_file_not_the_content_hash():
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        package_dir = os.path.join(folder, "yamldotnet", "16.3.0")
        os.makedirs(package_dir)
        with open(os.path.join(package_dir, "yamldotnet.16.3.0.nupkg.sha512"), "w") as f:
            f.write("FILEHASH==\n")
        lock = nuget_assets.build_lock(_report(_digest()), REPO, read_assets=lambda p: ASSETS,
                                       package_folders=[folder])
    # A signed package's content hash (assets file) is not the hash of the
    # .nupkg a feed serves; the file hash NuGet left beside the package is.
    assert lock["packages"]["yamldotnet/16.3.0"]["integrity"] == "sha512-FILEHASH=="
    # Without the package on disk the content hash is all there is...
    lock = nuget_assets.build_lock(_report(_digest()), REPO, read_assets=lambda p: ASSETS)
    assert lock["packages"]["yamldotnet/16.3.0"]["integrity"] == "sha512-CONTENTHASH=="
    # ...and a package with neither is reported, never fetched unverified.
    assert "stylecop.analyzers/1.1.118" in lock["unresolved"]


def test_runtime_identifier_selects_the_rid_specific_assets():
    assets = dict(ASSETS, targets=dict(ASSETS["targets"]))
    assets["targets"]["net8.0-windows10.0.26100.0/win-x64"] = {
        "YamlDotNet/16.3.0": {"type": "package", "compile": {"lib/net8.0/YamlDotNet.dll": {}},
                              "runtime": {"runtimes/win-x64/lib/net8.0/YamlDotNet.dll": {}}}}
    report = _report(dict(_digest(), runtime_identifier="win-x64"))
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: assets)
    assert lock["targets"]["Engine"]["framework"] == "net8.0-windows10.0.26100.0_win-x64"
    assert lock["packages"]["yamldotnet/16.3.0"]["frameworks"][
        "net8.0-windows10.0.26100.0_win-x64"]["runtime"] == ["runtimes/win-x64/lib/net8.0/YamlDotNet.dll"]


def test_targets_that_take_different_files_from_a_package_get_their_own_selection():
    # Two projects embed the C#/WinRT runtime and reference only the host shim;
    # one consumes the package normally. Handing all three the union would add
    # a reference (and with it a second copy of every runtime type) to two.
    def digest(*files):
        return dict(_digest(), references={"nuget": [
            {"id": "microsoft.windows.cswinrt", "version": "2.3.1", "path": f} for f in files]},
            analyzers={})
    shim, runtime = "lib/net8.0/WinRT.Host.Shim.dll", "lib/net8.0/WinRT.Runtime.dll"
    report = {"targets": {
        name: {"csharp": dict(digest(*files), target_framework="net8.0"), "steps": []}
        for name, files in (("A", [shim]), ("B", [shim]), ("C", [shim, runtime]))}}
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None)
    key = "microsoft.windows.cswinrt/2.3.1"
    assert lock["packages"][key]["frameworks"] == {
        "net8.0": {"compile": [shim]}, "net8.0_alt1": {"compile": [shim, runtime]}}
    assert [lock["targets"][n]["packages"][key] for n in "ABC"] == [
        "net8.0", "net8.0", "net8.0_alt1"]


def test_the_digest_says_which_files_came_from_which_pack():
    d = _digest()
    assert d["references"]["packs"] == {"microsoft.netcore.app.ref/8.0.31": 2}
    assert d["references"]["pack_files"] == {
        "microsoft.netcore.app.ref/8.0.31": ["ref/net8.0/System.Runtime.dll", "ref/net8.0/System.Linq.dll"]}
    assert d["references"]["pack_roots"] == {
        "microsoft.netcore.app.ref/8.0.31": "C:/Program Files/dotnet/packs/Microsoft.NETCore.App.Ref/8.0.31"}
    assert d["analyzers"]["pack_files"]["microsoft.netcore.app.ref/8.0.31"] == [
        "analyzers/dotnet/cs/System.Text.Json.SourceGeneration.dll"]


def test_what_rules_dotnet_cannot_reproduce():
    base = {"assembly": "PowerToys.Common", "target_framework": "net8.0-windows10.0.26100.0",
            "references": {"packs": {"microsoft.netcore.app.ref/8.0.31": 160}}}
    assert csharp.rules_dotnet_gaps(base) == []
    # .NET Framework: its whole reference pack where MSBuild passes a part.
    assert ".NET Framework" in csharp.rules_dotnet_gaps(dict(base, target_framework="net48"))[0]
    assert csharp.rules_dotnet_gaps(dict(base, target_framework="netstandard2.0")) == []
    web = dict(base, references={"packs": {"microsoft.aspnetcore.app.ref/8.0.31": 150}})
    assert "ASP.NET Core" in csharp.rules_dotnet_gaps(web)[0]
    # Resources: `<assembly>.<path>` is the only name it can give.
    resx = lambda name: {"name": name, "kind": "generated", "path": "src/Common/obj/x.resources",
                         "from": "src/Common/Properties/Resources.resx"}
    assert csharp.rules_dotnet_gaps(dict(base, resources=[resx("PowerToys.Common.Properties.Resources.resources")])) == []
    (gap,) = csharp.rules_dotnet_gaps(dict(base, resources=[resx("Common.Properties.Resources.resources"),
                                                            resx("Common.Other.resources")]))
    assert gap.startswith("embedded resource Common.Properties.Resources.resources (and 1 more)")
    plain = {"name": "PowerToys.Common.Assets.logo.png", "kind": "repo", "path": "src/Common/Assets/logo.png"}
    assert csharp.rules_dotnet_gaps(dict(base, resources=[plain]), "src/Common") == []
    # A file linked in from elsewhere, or given a LogicalName.
    assert csharp.rules_dotnet_gaps(dict(base, resources=[dict(plain, path="src/Shared/logo.png")]),
                                    "src/Common") != []
    assert csharp.rules_dotnet_gaps(dict(base, resources=[dict(plain, name="logo")]), "src/Common") != []


def test_a_pack_read_from_the_machine_is_locked_as_the_package_it_is():
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as work:
        pack = os.path.join(work, "packs", "Microsoft.WindowsDesktop.App.Ref", "8.0.31")
        os.makedirs(os.path.join(pack, "ref", "net8.0"))
        for name, data in (("PresentationCore.dll", b"core"), ("WindowsBase.dll", b"base")):
            with open(os.path.join(pack, "ref", "net8.0", name), "wb") as f:
                f.write(data)
        archive = os.path.join(work, "desktop.nupkg")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("ref/net8.0/PresentationCore.dll", b"core")
            z.writestr("ref/net8.0/WindowsBase.dll", b"DIFFERENT")
        key = "microsoft.windowsdesktop.app.ref/8.0.31"
        digest = dict(_digest(), assembly="Engine", resources=[],
                      target_framework="net8.0-windows10.0.26100.0")
        digest["references"] = dict(
            digest["references"],
            pack_files=dict(digest["references"]["pack_files"],
                            **{key: ["ref/net8.0/PresentationCore.dll", "ref/net8.0/WindowsBase.dll"]}),
            pack_roots=dict(digest["references"]["pack_roots"], **{key: pack.replace("\\", "/")}))
        report = {"targets": {"Engine": {"project": "src/Engine/Engine.csproj", "csharp": digest,
                                         "steps": []}}}
        fetched = []

        def fetch(package_id, version):
            fetched.append((package_id, version))
            return archive, "https://api.nuget.org/v3/index.json"

        lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None, fetch=fetch)
        # Not the toolchain's own pack: rules_dotnet brings that one.
        assert lock["targets"]["Engine"]["packs"] == {key: key}
        assert fetched == [("microsoft.windowsdesktop.app.ref", "8.0.31")]
        record = lock["packages"][key]
        assert record["integrity"].startswith("sha512-")
        assert record["frameworks"]["net8.0-windows10.0.26100.0"]["compile"] == [
            "ref/net8.0/PresentationCore.dll", "ref/net8.0/WindowsBase.dll"]
        # The package stands in for files the build read: they are compared.
        assert record["pack"] == {"of": key, "identical": 1, "unverified": [],
                                  "different": ["ref/net8.0/WindowsBase.dll"]}
        assert "https://api.nuget.org/v3/index.json" in lock["sources"]
        # Without a way to fetch it, it is reported, not guessed.
        lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None)
        assert key in lock["unresolved"]


def test_a_target_compiled_exactly_takes_even_the_toolchain_pack_from_the_lock():
    digest = dict(_digest(), target_framework="net48", resources=[])
    netfx = "Microsoft.NETFramework.ReferenceAssemblies/v4.8"
    digest["references"] = {"packs": {netfx: 2},
                            "pack_files": {netfx: ["mscorlib.dll", "Facades/System.Runtime.dll"]},
                            "pack_roots": {}}
    digest["analyzers"] = {}
    report = {"targets": {"Engine": {"project": "src/Engine/Engine.csproj", "csharp": digest, "steps": []}}}
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None)
    # The installed targeting pack has no version; its package does.
    key = "microsoft.netframework.referenceassemblies.net48/1.0.3"
    assert lock["targets"]["Engine"]["packs"] == {netfx: key}
    assert lock["packages"][key]["frameworks"]["net48"]["compile"] == [
        "build/.NETFramework/v4.8/Facades/System.Runtime.dll", "build/.NETFramework/v4.8/mscorlib.dll"]


def test_references_that_displace_an_assembly_of_the_toolchain_pack_are_marked():
    base, desktop = "microsoft.netcore.app.ref/8.0.31", "microsoft.windowsdesktop.app.ref/8.0.31"
    root = "C:/Program Files/dotnet/packs/Microsoft.NETCore.App.Ref/8.0.31"

    def target(files, extra=None):
        references = {"packs": {base: len(files)}, "pack_files": {base: list(files)},
                      "pack_roots": {base: root}}
        if extra:
            references["packs"][desktop] = len(extra)
            references["pack_files"][desktop] = list(extra)
        return {"project": "src/X/X.csproj", "steps": [], "csharp": {
            "assembly": "X", "target_framework": "net8.0", "references": references,
            "analyzers": {}}}

    plain = ["ref/net8.0/System.Runtime.dll", "ref/net8.0/WindowsBase.dll"]
    report = {"targets": {
        "Plain": target(plain),
        # MSBuild gave this one WindowsDesktop's WindowsBase and dropped the base pack's facade.
        "Wpf": target(["ref/net8.0/System.Runtime.dll"],
                      ["ref/net8.0/PresentationCore.dll", "ref/net8.0/WindowsBase.dll"])}}
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None,
                                   fetch=lambda i, v: None, list_directory=lambda p: [])
    chosen = lock["packages"][desktop]["frameworks"]["net8.0"]
    assert chosen["overrides"] == ["ref/net8.0/WindowsBase.dll"]
    assert lock["targets"]["Plain"]["packages"] == {}
    # If every project of the build is like that, the pack's own directory
    # still says what MSBuild left out.
    report = {"targets": {"Wpf": report["targets"]["Wpf"]}}
    listing = lambda p: ["System.Runtime.dll", "WindowsBase.dll", "System.Runtime.xml"]
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None,
                                   fetch=lambda i, v: None, list_directory=listing)
    assert lock["packages"][desktop]["frameworks"]["net8.0"]["overrides"] == ["ref/net8.0/WindowsBase.dll"]
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: None,
                                   fetch=lambda i, v: None, list_directory=lambda p: [])
    assert "overrides" not in lock["packages"][desktop]["frameworks"]["net8.0"]


def test_package_files_named_in_response_files_and_generated_configs_are_exported():
    metadata = NUGET + "\\microsoft.windows.sdk.win32metadata\\65.0.8\\Windows.Win32.winmd"
    steps = [{"kind": "write", "file": "src/Engine/obj/cswinrt.rsp", "lines": [
        '-input 10.0.26100.0\r\n-input "%s\\microsoft.windowsappsdk\\1.8.0\\lib\\uap10.0\\Microsoft.UI.winmd" '
        '"C:\\work\\proj\\x64\\Release\\Own.winmd"' % NUGET]}]
    resx = {(CWD + "\\" + OBJ + "\\Engine.Properties.Resources.resources").lower(): "src/Engine/R.resx"}
    report = _report(_digest(resource_sources=resx), steps)
    config = "build_property.CsWin32InputMetadataPaths = %s|C:\\elsewhere\\x.winmd\n" % metadata
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: ASSETS,
                                   read_text=lambda p: config if p.endswith(".editorconfig") else None)
    files = lock["targets"]["Engine"]["files"]
    assert files["microsoft.windowsappsdk/1.8.0"] == ["lib/uap10.0/Microsoft.UI.winmd"]
    assert files["microsoft.windows.sdk.win32metadata/65.0.8"] == ["Windows.Win32.winmd"]


def test_a_reference_a_packages_props_add_is_needed_at_run_time_too():
    # MSTest's adapter is not a NuGet asset of its package: the package's
    # .props adds it as a <Reference>, MSBuild copies it beside the test
    # program, and the program loads it. The assets file lists a placeholder.
    adapter = "buildTransitive/net9.0/Microsoft.VisualStudio.TestPlatform.MSTest.TestAdapter.dll"
    assets = {
        "libraries": {"MSTest.TestAdapter/3.8.3": {"type": "package"},
                      "System.Runtime.WindowsRuntime/4.6.0": {"type": "package"}},
        "targets": {"net8.0": {
            "MSTest.TestAdapter/3.8.3": {"type": "package", "compile": {"lib/net9.0/_._": {}},
                                         "runtime": {"lib/net9.0/_._": {}}},
            "System.Runtime.WindowsRuntime/4.6.0": {"type": "package", "compile": {"lib/net8.0/_._": {}}}}},
        "project": {"frameworks": {"net8.0": {}}},
    }
    digest = {"assembly": "Tests", "target_framework": "net8.0", "analyzers": {}, "target": "exe",
              "assets_file": "src/Tests/obj/project.assets.json",
              "references": {"nuget": [
                  {"id": "mstest.testadapter", "version": "3.8.3", "path": adapter},
                  # A reference assembly is compiled against and never loaded.
                  {"id": "system.runtime.windowsruntime", "version": "4.6.0",
                   "path": "ref/netstandard2.0/System.Runtime.WindowsRuntime.dll"}]}}
    report = {"targets": {"Tests": {"project": "src/Tests/Tests.csproj", "csharp": digest, "steps": []}}}
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: assets)
    assert lock["packages"]["mstest.testadapter/3.8.3"]["frameworks"]["net8.0"] == {
        "compile": [adapter], "runtime": [adapter]}
    assert lock["packages"]["system.runtime.windowsruntime/4.6.0"]["frameworks"]["net8.0"] == {
        "compile": ["ref/netstandard2.0/System.Runtime.WindowsRuntime.dll"]}


def test_run_time_files_belong_to_programs_not_to_libraries():
    # MSBuild deploys a program's own restore graph beside it; a library
    # deploys nothing. A library's selection of a package is what it compiles
    # against, so that it cannot replace a program's choice of what to load.
    program = nuget_assets.build_lock(_report(_digest()), REPO, read_assets=lambda p: ASSETS)
    library = nuget_assets.build_lock(_report(_digest(), target="library"), REPO,
                                      read_assets=lambda p: ASSETS)
    name = "net8.0-windows10.0.26100.0"
    assert program["packages"]["yamldotnet/16.3.0"]["frameworks"][name] == {
        "compile": ["lib/net8.0/YamlDotNet.dll"], "runtime": ["lib/net8.0/YamlDotNet.dll"]}
    assert library["packages"]["yamldotnet/16.3.0"]["frameworks"][name] == {
        "compile": ["lib/net8.0/YamlDotNet.dll"]}
    # Nothing to compile against and nothing to deploy: not a dependency at all.
    assert "native.runtime/1.0.0" in program["targets"]["Engine"]["packages"]
    assert "native.runtime/1.0.0" not in library["targets"]["Engine"]["packages"]


def test_what_msbuild_copied_beside_the_program_is_its_run_time_closure():
    # More than the assets file's run-time list: files a package's .targets
    # copy (MSTest's platform services), satellite assemblies, native code.
    package = (NUGET + "\\mstest.testadapter\\3.8.3\\").replace("\\", "/")
    out = "x64/Release/tests/Engine/"
    steps = [{"kind": "copy",
              "from": [package + "buildTransitive/net9.0/PlatformServices.dll",
                       package + "buildTransitive/net9.0/de/PlatformServices.resources.dll",
                       package + "runtimes/win-x64/native/shim.dll",
                       package + "lib/net9.0/testhost.exe",              # a launcher, not an assembly to load
                       package + "build/tools/amd64/profiler.dll",       # laid out in a subdirectory
                       package + "embedded/Source.cs"],                  # into obj/: a source, not deployed
              "to": [out + "PlatformServices.dll", out + "de/PlatformServices.resources.dll",
                     out + "shim.dll", out + "testhost.exe", out + "tools/amd64/profiler.dll",
                     "src/Engine/obj/Source.cs"]}]
    report = _report(dict(_digest(), resources=[]), steps)
    report["targets"]["Engine"]["output"] = out + "Engine.dll"
    lock = nuget_assets.build_lock(report, REPO, read_assets=lambda p: ASSETS)
    chosen = lock["packages"]["mstest.testadapter/3.8.3"]["frameworks"]["net8.0-windows10.0.26100.0"]
    assert chosen == {"runtime": ["buildTransitive/net9.0/PlatformServices.dll"],
                      "resources": ["buildTransitive/net9.0/de/PlatformServices.resources.dll"],
                      "native": ["runtimes/win-x64/native/shim.dll"]}
    assert lock["targets"]["Engine"]["files"]["mstest.testadapter/3.8.3"] == ["embedded/Source.cs"]
    # The feeds are still the feeds.
    assert lock["sources"] == ["https://feed.example/v3/index.json"]


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
