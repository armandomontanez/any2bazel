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

"""emit_dotnet.py: report + lock -> rules_dotnet BUILD files."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import emit_dotnet
from emit_dotnet import Emitter

TFM = "net8.0-windows10.0.26100.0"


def _digest(assembly, **over):
    d = {
        "assembly": assembly, "target": "library", "target_framework": TFM,
        "defines": ["NET", "NET8_0", "RELEASE", "TRACE", "WINDOWS"],
        "options": ["/deterministic", "/highentropyva", "/langversion:12.0", "/nostdlib",
                    "/nowarn:CS1701", "/target:library"],
        "sources": {"hand_written": ["src/%s/A.cs" % assembly]},
        "references": {"packs": {"microsoft.netcore.app.ref/8.0.31": 160}},
        "analyzers": {},
    }
    d.update(over)
    return d


def _report(*digests, steps=None, extra=None):
    targets = {}
    for d in digests:
        name = d.pop("_name", d["assembly"])
        project = d.pop("_project", "src/%s/%s.csproj" % (d["assembly"], d["assembly"]))
        targets[name] = {"project": project, "csharp": d, "steps": (steps or {}).get(name, [])}
    targets.update(extra or {})
    return {"targets": targets}


# A lock in which the Engine target also takes its framework pack from a package.
EXACT_LOCK = {"targets": {"Engine": {
    "framework": TFM, "packs": {"microsoft.netcore.app.ref/8.0.31": "microsoft.netcore.app.ref/8.0.31"},
    "packages": {"microsoft.netcore.app.ref/8.0.31": TFM, "yamldotnet/16.3.0": TFM + "_alt1"}}}}


def _emit(report, lock=None, **kw):
    with tempfile.TemporaryDirectory() as root:
        emitter = Emitter(report, lock or {"targets": {}}, root, **kw)
        return emitter, emitter.emit()


def test_options_become_the_attributes_rules_dotnet_has_for_them():
    d = _digest("Engine", options=[
        "/deterministic", "/doc", "/highentropyva", "/langversion:12.0", "/nostdlib",
        "/nowarn:CS1591", "/nullable:enable", "/platform:x64", "/target:library", "/unsafe",
        "/warn:8", "/warnaserror", "/warnaserror:NU1605", "/warnaserror-:CS0618"],
        defines=["NET", "NET8_0", "POWERSHELL_WINDOWS", "RELEASE", "TRACE", "WINDOWS"])
    emitter, files = _emit(_report(d))
    text = files["src/Engine/BUILD.bazel"]
    assert text.startswith(emit_dotnet.MARKER)
    assert 'load("@rules_dotnet//dotnet:defs.bzl", "csharp_library")' in text
    for line in ('    name = "Engine",', '    out = "Engine",', '    langversion = "12.0",',
                 '    nullable = "enable",', "    allow_unsafe_blocks = True,",
                 "    generate_documentation_file = True,", "    treat_warnings_as_errors = True,",
                 '    target_frameworks = ["net8.0"],'):
        assert line in text, line
    # The platform is not a rules_dotnet framework: it is stated through defines.
    assert 'defines = MSBUILD_FRAMEWORK_DEFINES["%s"] + [\n        "POWERSHELL_WINDOWS",' % TFM in text
    assert emitter.framework_defines[TFM] == ["NET", "NET8_0", "WINDOWS"]
    # No attribute for these: straight to the compiler. `/warn:8` exceeds the
    # attribute's range, and a list of ids cannot sit next to "all".
    assert ('compiler_options = [\n        "/platform:x64",\n        "/warn:8",\n'
            '        "/warnaserror+:NU1605",\n    ]') in text
    assert 'warnings_not_as_errors = [\n        "CS0618",' in text
    # What rules_dotnet states on every line anyway is not repeated.
    assert "/deterministic" not in text and "/nostdlib" not in text


def test_compiler_defaults_are_stated_because_rules_dotnet_has_other_defaults():
    _, files = _emit(_report(_digest("Engine")))
    text = files["src/Engine/BUILD.bazel"]
    assert "    warning_level = 4," in text            # csc: 4, rules_dotnet: 3
    assert '    nullable = "disable",' in text
    assert "    generate_documentation_file = False," in text


def test_packages_projects_and_loose_files_become_labels():
    shared = _digest("Shared", target_framework="netstandard2.0",
                     defines=["NETSTANDARD", "NETSTANDARD2_0"])
    engine = _digest(
        "Engine",
        sources={"hand_written": ["src/Engine/A.cs", "src/Common/Linked.cs"],
                 "external": [{"kind": "nuget", "id": "microsoft.net.test.sdk", "version": "17.14.1",
                               "path": "build/net8.0/Program.cs"}]},
        references={"projects": [{"target": "Shared", "path": "src/Shared/obj/ref/Shared.dll"}]},
        analyzers={"sdk": ["Sdks/Microsoft.NET.Sdk/analyzers/Microsoft.CodeAnalysis.NetAnalyzers.dll"]},
        additional_files=[{"kind": "repo", "path": "src/stylecop.json"}],
        analyzer_configs=[{"kind": "repo", "path": ".editorconfig"},
                          {"kind": "sdk", "path": "Sdks/Microsoft.NET.Sdk/analyzers/build/config/analysislevel_8_default.globalconfig"}])
    lock = {"targets": {"Engine": {"framework": TFM, "packages": {"yamldotnet/16.3.0": TFM}}}}
    emitter, files = _emit(_report(shared, engine), lock)
    text = files["src/Engine/BUILD.bazel"]
    for label in ('"@nuget//yamldotnet/16.3.0:%s",' % TFM, '"//src/Shared:Shared",',
                  '"//bazel/dotnet:sdk_analyzers_net",', '"//:src/Common/Linked.cs",',
                  '"@nuget//microsoft.net.test.sdk/17.14.1:build/net8.0/Program.cs",',
                  '"//:src/stylecop.json",', '"//:.editorconfig",',
                  '"//bazel/dotnet:sdk_config_analysislevel_8_default",'):
        assert label in text, label
    # Files another package takes are exported from where they live.
    assert emitter.workspace.exports == {"": {"src/Common/Linked.cs", "src/stylecop.json",
                                              ".editorconfig"}}
    shared_build = files["bazel/dotnet/BUILD.bazel"]
    assert 'name = "sdk_analyzers_net_files"' in shared_build
    assert 'name = "sdk_config_analysislevel_8_default"' in shared_build


def test_a_multi_targeted_project_gets_one_target_per_framework():
    a = _digest("Engine", _name="Engine[netstandard2.0]", target_framework="netstandard2.0",
                defines=["NETSTANDARD"],
                references={"packs": {"netstandard.library/2.0.3": 100}})
    b = _digest("Engine", _name="Engine[%s]" % TFM)
    _, files = _emit(_report(a, b))
    text = files["src/Engine/BUILD.bazel"]
    assert 'name = "Engine_netstandard2.0",' in text and 'name = "Engine_%s",' % TFM in text
    assert text.count('out = "Engine",') == 2
    assert 'target_frameworks = ["netstandard2.0"],' in text
    assert 'target_frameworks = ["net8.0"],' in text


def test_what_cannot_be_expressed_is_reported_and_not_written():
    desktop = _digest("Desktop", references={"packs": {"microsoft.windowsdesktop.app.ref/8.0.31": 9}})
    odd = _digest("Odd", sources={"hand_written": ["src/Odd/A.cs"],
                                  "generated": [{"kind": "generated", "path": "src/Odd/obj/x/G.cs"}]})
    user = _digest("User", references={"projects": [{"target": "Odd", "path": "x/Odd.dll"}]})
    fine = _digest("Fine")
    emitter, files = _emit(_report(desktop, odd, user, fine))
    assert sorted(f for f in files if f.startswith("src/")) == ["src/Fine/BUILD.bazel"]
    # A pack rules_dotnet has no notion of must come through the lock.
    assert "the lock does not have" in emitter.summary["Desktop"]["unhandled"][0]
    assert "generated sources (generated)" in emitter.summary["Odd"]["unhandled"][0]
    # A dependent of something unwritten is unwritten too, and says why.
    assert emitter.summary["User"]["unhandled"] == ["depends on Odd, which was not emitted"]
    assert "unhandled" not in emitter.summary["Fine"]


def test_msbuild_generated_files_are_restated_with_the_reference_content():
    with tempfile.TemporaryDirectory() as reference:
        obj = os.path.join(reference, "src", "Engine", "obj")
        os.makedirs(obj)
        with open(os.path.join(obj, "Engine.AssemblyInfo.cs"), "w", encoding="utf-8-sig") as f:
            f.write('[assembly: System.Reflection.AssemblyVersionAttribute("1.0.0.0")]\n')
        with open(os.path.join(obj, "Engine.editorconfig"), "w", encoding="utf-8") as f:
            f.write("is_global = true\nbuild_property.ProjectDir = %s\\src\\Engine\\\n" % reference)
        d = _digest("Engine",
                    sources={"hand_written": ["src/Engine/A.cs"],
                             "generated": [{"kind": "assembly_info",
                                            "path": "src/Engine/obj/Engine.AssemblyInfo.cs"}]},
                    analyzer_configs=[{"kind": "generated", "path": "src/Engine/obj/Engine.editorconfig"}])
        emitter, files = _emit(_report(d), reference_root=reference)
    text = files["src/Engine/BUILD.bazel"]
    # The two builds' copies are paired for the diff: dropping one is an error.
    assert emitter.config_suggestions()["ignore"]["source_map"] == [
        {"from": "src/Engine/obj/Engine.AssemblyInfo.cs", "to": "@msbuild/Engine/assembly_info"},
        {"from": "bazel-bin/src/Engine/_msbuild/Engine/Engine.AssemblyInfo.cs",
         "to": "@msbuild/Engine/assembly_info"}]
    assert 'load("@bazel_skylib//rules:write_file.bzl", "write_file")' in text
    assert '"[assembly: System.Reflection.AssemblyVersionAttribute(\\"1.0.0.0\\")]",' in text
    assert '":Engine_assembly_info",' in text and '":Engine_msbuild_editorconfig",' in text
    # A path of the reference tree is not carried into the Bazel build.
    assert '"build_property.ProjectDir = src/Engine/",' in text
    # Without the reference tree the content is unknown, which is a reason not to emit.
    d = _digest("Engine", sources={"hand_written": [], "generated": [
        {"kind": "assembly_info", "path": "src/Engine/obj/Engine.AssemblyInfo.cs"}]})
    emitter, _ = _emit(_report(d))
    assert "reference file not readable" in emitter.summary["Engine"]["unhandled"][0]


def test_resx_resources_are_converted_and_named_like_the_reference():
    resource = {"name": "Engine.Properties.Resources.resources", "kind": "generated",
                "path": "src/Engine/obj/Engine.Properties.Resources.resources",
                "from": "src/Engine/Properties/Resources.resx"}
    _, files = _emit(_report(_digest("Engine", resources=[resource])))
    text = files["src/Engine/BUILD.bazel"]
    assert 'load("//bazel/dotnet:resx.bzl", "resx_resources")' in text
    assert ('resx_resources(\n    name = "Properties.Resources_resx",\n'
            '    src = "Properties/Resources.resx",\n    out = "Properties.Resources.resources",\n)') in text
    assert 'resources = [\n        ":Properties.Resources_resx",' in text
    # rules_dotnet names a resource after the assembly. Under any other name
    # (a root namespace that is not the assembly name) the project is compiled
    # by the exact-command-line rule, which is told the name.
    resource = dict(resource, name="Other.Namespace.Resources.resources")
    emitter, files = _emit(_report(_digest("Engine", resources=[resource])), EXACT_LOCK)
    text = files["src/Engine/BUILD.bazel"]
    assert "rules_dotnet's own rules cannot reproduce this compile" in text
    assert 'load("//bazel/dotnet:csc_assembly.bzl", "exact_csharp_assembly")' in text
    assert '    out = "Other.Namespace.Resources.resources",' in text
    assert ('    resources = {\n        ":Other.Namespace.Resources_resx": '
            '"Other.Namespace.Resources.resources",\n    },') in text
    assert emitter.summary["Engine"]["exact"][0].startswith("embedded resource Other.Namespace")


def test_winrt_chain_metadata_projection_and_direct_references():
    native = {"Component": {"project": "src/Component/Component.vcxproj", "winrt": {
        "idl": ["src/Component/Api.idl"], "midl_options": ["/W1", "/env", "x64"],
        "winmd": "src/Component/x64/Release/Merged/My.Component.winmd",
        "copies": ["x64/Release/Component/My.Component.winmd"]}},
        "Other": {"project": "src/Other/Other.vcxproj", "winrt": {
            "idl": ["src/Other/Other.idl"], "midl_options": [],
            "winmd": "src/Other/x64/Release/Merged/My.Other.winmd",
            "copies": ["x64/Release/Other/My.Other.winmd"]}}}
    out_dir = "src/Engine/obj/Generated Files/CsWinRT"
    # One -input takes several files, as the CsWinRT targets write it.
    steps = {"Engine": [
        {"kind": "write", "file": out_dir + "/cswinrt.rsp", "lines": [
            '-target net8.0\r\n-input 10.0.26100.0\r\n-input "C:\\ref\\x64\\Release\\Component\\My.Component.winmd" '
            '"C:\\ref\\x64\\Release\\Other\\My.Other.winmd"\r\n'
            '-output "C:\\ref\\src\\Engine\\obj\\Generated Files\\CsWinRT"\r\n-exclude Windows\r\n'
            '-include My.Component\r\n\r\n-embedded']},
        {"kind": "exec", "command": '"C:\\Users\\dev\\.nuget\\packages\\microsoft.windows.cswinrt\\2.3.1\\cswinrt.exe" '
                                    '@"C:\\ref\\src\\Engine\\obj\\Generated Files\\CsWinRT\\cswinrt.rsp"'}]}
    engine = _digest("Engine", sources={"hand_written": ["src/Engine/A.cs"], "generated": [
        {"kind": "cswinrt_projection", "dir": out_dir, "files": 28}]})
    legacy = _digest("Legacy", target_framework="net48", defines=["NET48"],
                     references={"other": ["x64/Release/Component/My.Component.winmd"]})
    emitter, files = _emit(_report(engine, legacy, steps=steps, extra=native), reference_root="C:\\ref")
    text = files["src/Engine/BUILD.bazel"]
    assert 'load("//bazel/dotnet:cswinrt.bzl", "cswinrt_projection")' in text
    for line in ('    name = "Engine_cswinrt",',
                 '    cswinrt = "@nuget//microsoft.windows.cswinrt/2.3.1:cswinrt.exe",',
                 '    sdk_winmds = ["@windows_sdk//:platform_winmds"],', '    target = "net8.0",',
                 '        "//bazel/winmd:My.Component",', '        "//bazel/winmd:My.Other",',
                 '    opts = [\n        "-embedded",\n    ],', '        ":Engine_cswinrt",'):
        assert line in text, line
    assert "C:\\\\ref" not in text      # no path of the reference tree
    # .NET Framework references the metadata itself (as a file: such a target
    # is compiled by the exact rule).
    assert '"//bazel/winmd:My.Component",' in files["src/Legacy/BUILD.bazel"]
    winmd = files["bazel/winmd/BUILD.bazel"]
    assert 'winrt_winmd(\n    name = "My.Component",' in winmd
    assert '"//:src/Component/Api.idl",' in winmd and 'namespace = "My.Component",' in winmd
    # How the two builds' generated trees correspond, for the diff.
    assert emitter.config_suggestions()["ignore"]["source_map"] == [
        {"from": out_dir + "/", "to": "@cswinrt/Engine"},
        {"from": "bazel-bin/src/Engine/cswinrt.cs", "to": "@cswinrt/Engine"}]
    # The directory is short on purpose: cswinrt.exe writes nothing, and says
    # nothing, when a file's path exceeds MAX_PATH.
    assert '    out = "cswinrt.cs",' in text


def test_project_specific_generators_are_supplied_by_the_operator():
    d = _digest("Engine", sources={"hand_written": ["src/Engine/A.cs"], "generated": [
        {"kind": "generated", "path": "src/Engine/obj/Generated Files/Wrapped/X.cs"},
        {"kind": "generated", "path": "src/Engine/obj/Generated Files/Wrapped/Y.cs"}]})
    hook = {"match": "/Generated Files/Wrapped/", "srcs": [":{target}_wrapped"],
            "load": {"//bazel/project:wrap.bzl": ["wrapped"]},
            "starlark": 'wrapped(\n    name = "{target}_wrapped",\n)',
            "bazel_output": "{target}_wrapped.cs"}
    emitter, files = _emit(_report(d), hooks=[hook])
    text = files["src/Engine/BUILD.bazel"]
    assert 'load("//bazel/project:wrap.bzl", "wrapped")' in text
    assert text.count('wrapped(\n    name = "Engine_wrapped",') == 1       # once, not per file
    assert '":Engine_wrapped",' in text
    assert {"from": "bazel-bin/src/Engine/Engine_wrapped.cs", "to": "@generated/Engine/0"} in \
        emitter.config_suggestions()["ignore"]["source_map"]


def test_only_marked_files_are_overwritten_and_exports_are_merged():
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "src", "Engine"))
        mine = os.path.join(root, "src", "Engine", "BUILD.bazel")
        with open(mine, "w") as f:
            f.write("# hand-written\n")
        with open(os.path.join(root, "BUILD.bazel"), "w") as f:
            f.write('exports_files(["LICENSE"])\n')
        skipped = emit_dotnet.write({"src/Engine/BUILD.bazel": emit_dotnet.MARKER + "\n",
                                     "src/New/BUILD.bazel": emit_dotnet.MARKER + "\n"},
                                    {"": {"b.json", "a.cs"}}, root)
        assert skipped == ["src/Engine/BUILD.bazel"]
        assert open(mine).read() == "# hand-written\n"
        assert os.path.isfile(os.path.join(root, "src", "New", "BUILD.bazel"))
        text = open(os.path.join(root, "BUILD.bazel")).read()
        assert text.startswith('exports_files(["LICENSE"])\n')
        assert '    "a.cs",\n    "b.json",' in text
        # A second run replaces its own block instead of appending another.
        emit_dotnet.write({}, {"": {"c.cs"}}, root)
        text = open(os.path.join(root, "BUILD.bazel")).read()
        assert text.count(emit_dotnet.EXPORTS_BEGIN) == 1 and '"c.cs"' in text and '"a.cs"' not in text


def test_a_target_rules_dotnet_cannot_reproduce_is_compiled_with_the_exact_command_line():
    shared = _digest("Shared", target_framework="netstandard2.0", defines=["NETSTANDARD"],
                     references={"packs": {"netstandard.library/2.0.3": 100}})
    helper = _digest("Helper", target_framework="net48", defines=["NET48", "NETFRAMEWORK"],
                     references={})
    engine = _digest(
        "Engine", target_framework="net48", target="winexe",
        defines=["NET48", "NETFRAMEWORK", "RELEASE", "TRACE", "FEATURE_X"],
        options=["/deterministic", "/doc", "/highentropyva", "/langversion:10.0", "/nostdlib",
                 "/nowarn:CS1591", "/nullable:enable", "/target:winexe", "/warnaserror",
                 "/warnaserror-:CsWinRT1028", "/win32icon:src/Engine/app.ico"],
        references={"packs": {"Microsoft.NETFramework.ReferenceAssemblies/v4.8": 117},
                    "projects": [{"target": "Shared", "path": "src/Shared/obj/ref/Shared.dll"},
                                 {"target": "Helper", "path": "src/Helper/obj/Helper.dll"}],
                    "other": ["x64/Release/Component/My.Component.winmd"]},
        analyzers={"sdk": ["Sdks/Microsoft.NET.Sdk/analyzers/Microsoft.CodeAnalysis.NetAnalyzers.dll"]},
        resources=[{"name": "Engine.Images.logo.png", "kind": "repo", "path": "src/Assets/logo.png"}])
    native = {"Component": {"project": "src/Component/Component.vcxproj", "winrt": {
        "idl": ["src/Component/Api.idl"], "midl_options": [],
        "winmd": "src/Component/x64/Release/Merged/My.Component.winmd",
        "copies": ["x64/Release/Component/My.Component.winmd"]}}}
    netfx = "microsoft.netframework.referenceassemblies.net48/1.0.3"
    lock = {"targets": {"Engine": {
        "framework": "net48", "packs": {"Microsoft.NETFramework.ReferenceAssemblies/v4.8": netfx},
        "packages": {netfx: "net48", "newtonsoft.json/13.0.3": "net48"}}}}
    emitter, files = _emit(_report(shared, helper, engine, extra=native), lock)
    assert emitter.summary["Engine"]["rule"] == "exact_csharp_assembly"
    assert emitter.summary["Shared"]["rule"] == "csharp_library"
    text = files["src/Engine/BUILD.bazel"]
    assert "#   .NET Framework target: rules_dotnet passes its whole reference pack" in text
    for line in (
            '    assembly_name = "Engine",', '    kind = "winexe",', '    target_framework = "net48",',
            "    generate_documentation_file = True,", '    win32_icon = "app.ico",',
            # References are files: package selections, the framework pack
            # among them, the compile behind another exact target, a
            # rules_dotnet library, WinRT metadata.
            '        "@nuget//%s:net48.refs",' % netfx, '        "@nuget//newtonsoft.json/13.0.3:net48.refs",',
            '        "//src/Helper:Helper_csc",', '        "//src/Shared:Shared",',
            '        "//bazel/winmd:My.Component",',
            '        "@nuget//newtonsoft.json/13.0.3:net48.analyzers",',
            '        "//bazel/dotnet:sdk_analyzers_net_files",',
            # Whatever name the reference build gave it.
            '        "//:src/Assets/logo.png": "Engine.Images.logo.png",'):
        assert line in text, line
    # Options go to the compiler as the reference build spelled them, ids included.
    assert ('    options = [\n        "/deterministic",\n        "/highentropyva",\n'
            '        "/langversion:10.0",\n        "/nostdlib",\n        "/nowarn:CS1591",\n'
            '        "/nullable:enable",\n        "/warnaserror",\n'
            '        "/warnaserror-:CsWinRT1028",\n    ],') in text
    # TRACE is the project's; only the build type is the rule's.
    assert '"FEATURE_X",' in text and '"TRACE",' in text and '"RELEASE"' not in text
    # What comes along at run time is still said in rules_dotnet's terms, and
    # the framework pack is not part of it.
    deps = text[text.index("    deps = ["):text.index("]", text.index("    deps = ["))]
    assert '"@nuget//newtonsoft.json/13.0.3:net48",' in deps and '"//src/Helper:Helper",' in deps
    assert netfx not in deps
    assert 'exports_files(["csc_wrapper.bat"])' in files["bazel/dotnet/BUILD.bazel"]
    # Without the pack in the lock there is nothing to compile against.
    emitter, _ = _emit(_report(_digest("Engine", target_framework="net48",
                                       references={"packs": {"X.Ref/v4.8": 1}})))
    assert "is not in the lock" in emitter.summary["Engine"]["unhandled"][0]


def test_the_resx_converter_is_not_an_assembly_of_the_project():
    resource = {"name": "Engine.Properties.Resources.resources", "kind": "generated",
                "path": "src/Engine/obj/Engine.Properties.Resources.resources",
                "from": "src/Engine/Properties/Resources.resx"}
    emitter, _ = _emit(_report(_digest("Engine", resources=[resource])))
    assert emitter.config_suggestions()["exclude_targets"] == ["bazel/dotnet/resx:resx2resources"]
    emitter, _ = _emit(_report(_digest("Engine")))
    assert "exclude_targets" not in emitter.config_suggestions()


def test_files_a_resx_links_to_are_inputs_of_its_conversion():
    resx = ('<root>\n'
            '  <data name="$this.Icon" type="System.Resources.ResXFileRef, System.Windows.Forms">\n'
            '    <value>..\\Assets\\app.ico;System.Drawing.Icon, System.Drawing</value>\n'
            '  </data>\n'
            '  <data name="Title"><value>hello</value></data>\n'
            '</root>\n')
    resource = {"name": "Engine.Forms.Main.resources", "kind": "generated",
                "path": "src/Engine/obj/Engine.Forms.Main.resources",
                "from": "src/Engine/Forms/Main.resx"}
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "src", "Engine", "Forms"))
        with open(os.path.join(root, "src", "Engine", "Forms", "Main.resx"), "w", encoding="utf-8") as f:
            f.write(resx)
        emitter = Emitter(_report(_digest("Engine", resources=[resource])), {"targets": {}}, root)
        text = emitter.emit()["src/Engine/BUILD.bazel"]
    # The conversion reads the icon; the string entry names no file.
    assert ('resx_resources(\n    name = "Forms.Main_resx",\n    src = "Forms/Main.resx",\n'
            '    out = "Forms.Main.resources",\n    linked = [\n        "Assets/app.ico",\n    ],\n)') in text


def test_a_file_a_compiler_option_names_is_an_input():
    d = _digest("Engine", target="winexe", options=[
        "/langversion:12.0", "/target:winexe", "/win32manifest:src/Engine/app.manifest"])
    _, files = _emit(_report(d))
    text = files["src/Engine/BUILD.bazel"]
    # rules_dotnet has no attribute for it: a path for the compiler, and the
    # file declared so that the action has it.
    assert '"/win32manifest:src/Engine/app.manifest",' in text
    assert 'compile_data = [\n        "app.manifest",\n    ],' in text
    # One MSBuild generated cannot be given to rules_dotnet as a path...
    generated = "/win32manifest:src/Engine/obj/x64/Release/Manifests/app.manifest"
    emitter, _ = _emit(_report(dict(d, options=d["options"][:2] + [generated])))
    assert "names a file the build generates" in emitter.summary["Engine"]["unhandled"][0]
    # ...but csc_assembly takes a label, and the file is restated.
    with tempfile.TemporaryDirectory() as reference:
        manifests = os.path.join(reference, "src", "Engine", "obj", "x64", "Release", "Manifests")
        os.makedirs(manifests)
        with open(os.path.join(manifests, "app.manifest"), "w", encoding="utf-8") as f:
            f.write('<assembly manifestVersion="1.0"/>\n')
        d = _digest("Engine", target="winexe", target_framework="net48", references={},
                    options=["/target:winexe", generated])
        _, files = _emit(_report(d), reference_root=reference)
    text = files["src/Engine/BUILD.bazel"]
    assert 'win32_manifest = ":Engine_app.manifest",' in text
    assert ('write_file(\n    name = "Engine_app.manifest",\n'
            '    out = "_msbuild/Engine/app.manifest",') in text


def test_package_files_named_in_the_generated_config_become_labels():
    cache = "C:\\Users\\dev\\.nuget\\packages\\microsoft.windows.sdk.win32metadata\\65.0.8"
    with tempfile.TemporaryDirectory() as reference:
        obj = os.path.join(reference, "src", "Engine", "obj")
        os.makedirs(obj)
        with open(os.path.join(obj, "Engine.editorconfig"), "w", encoding="utf-8") as f:
            f.write("is_global = true\n"
                    "build_property.CsWin32InputMetadataPaths = %s\\Windows.Win32.winmd|%s\\Other.winmd\n"
                    "build_property.RootNamespace = Engine\n" % (cache, cache))
        d = _digest("Engine", analyzer_configs=[
            {"kind": "generated", "path": "src/Engine/obj/Engine.editorconfig"}])
        _, files = _emit(_report(d), reference_root=reference)
    text = files["src/Engine/BUILD.bazel"]
    package = "@nuget//microsoft.windows.sdk.win32metadata/65.0.8"
    # No path of the machine that ran the reference build...
    assert "C:\\\\Users" not in text
    assert '"build_property.CsWin32InputMetadataPaths = {{0}}|{{1}}",' in text
    assert 'load("//bazel/dotnet:msbuild_config.bzl", "msbuild_editorconfig")' in text
    assert ('    files = {\n        "%s:Other.winmd": "{{1}}",\n'
            '        "%s:Windows.Win32.winmd": "{{0}}",\n    },' % (package, package)) in text
    # ...and the files reach the compile, which is where the generator opens them.
    assert ('compile_data = [\n        "%s:Other.winmd",\n        "%s:Windows.Win32.winmd",\n    ],'
            % (package, package)) in text


def test_a_project_that_compiles_xaml_is_excluded_not_unhandled():
    app = _digest("App", resources=[{"name": "App.g.resources", "kind": "generated",
                                     "path": "src/App/obj/App.g.resources"}])
    user = _digest("App.Tests", references={"projects": [{"target": "App", "path": "x/App.dll"}]})
    odd = _digest("Odd", sources={"hand_written": [], "generated": [
        {"kind": "generated", "path": "src/Odd/obj/G.cs"}]})
    emitter, files = _emit(_report(app, user, odd))
    assert [f for f in files if f.startswith("src/")] == []
    assert emitter.summary["App"]["excluded"].startswith("XAML: the project compiles markup")
    assert emitter.summary["App.Tests"]["excluded"] == "depends on App, which is excluded (XAML)"
    assert "excluded" not in emitter.summary["Odd"] and "unhandled" in emitter.summary["Odd"]


def test_a_reference_that_displaces_a_framework_assembly_is_a_direct_dependency():
    desktop = "microsoft.windowsdesktop.app.ref/8.0.31"
    lock = {"packages": {desktop: {"frameworks": {TFM: {
                "compile": ["ref/net8.0/PresentationCore.dll", "ref/net8.0/WindowsBase.dll"],
                "overrides": ["ref/net8.0/WindowsBase.dll"]}}}},
            "targets": {"Engine": {"framework": TFM, "packs": {desktop: desktop},
                                   "packages": {desktop: TFM}}}}
    d = _digest("Engine", references={"packs": {"microsoft.netcore.app.ref/8.0.31": 165, desktop: 2}})
    _, files = _emit(_report(d), lock)
    text = files["src/Engine/BUILD.bazel"]
    # rules_dotnet drops its own WindowsBase only for a direct dependency of that name.
    assert '"@nuget//%s:%s",' % (desktop, TFM) in text
    assert '"@nuget//%s:%s.as.WindowsBase",' % (desktop, TFM) in text


def test_a_windows_subsystem_program_is_told_to_the_compiler_directly():
    d = _digest("App", target="winexe", options=["/langversion:12.0", "/target:winexe"])
    _, files = _emit(_report(d))
    text = files["src/App/BUILD.bazel"]
    # rules_dotnet declares `winexe` and compiles with /target:exe all the same.
    assert "    winexe = True," in text
    assert 'compiler_options = [\n        "/target:winexe",\n    ],' in text
    _, files = _emit(_report(_digest("Tool", target="exe", options=["/target:exe"])))
    assert "/target:" not in files["src/Tool/BUILD.bazel"]


def test_projections_of_a_multi_targeted_project_do_not_share_a_directory():
    out_dir = "src/Engine/obj/%s/Generated Files/CsWinRT"
    steps, digests = {}, []
    for tfm in ("net8.0-windows10.0.26100.0", "net9.0-windows10.0.26100.0"):
        name = "Engine[%s]" % tfm
        steps[name] = [
            {"kind": "write", "file": out_dir % tfm + "/cswinrt.rsp", "lines": [
                '-target net8.0\r\n-input 10.0.26100.0\r\n-output "C:\\ref\\%s"' % (out_dir % tfm).replace("/", "\\")]},
            {"kind": "exec", "command": '"C:\\Users\\dev\\.nuget\\packages\\microsoft.windows.cswinrt\\2.3.1\\cswinrt.exe" '
                                        '@"C:\\ref\\%s\\cswinrt.rsp"' % (out_dir % tfm).replace("/", "\\")}]
        digests.append(_digest("Engine", _name=name, target_framework=tfm, sources={
            "hand_written": ["src/Engine/A.cs"],
            "generated": [{"kind": "cswinrt_projection", "dir": out_dir % tfm, "files": 1}]}))
    _, files = _emit(_report(*digests, steps=steps), reference_root="C:\\ref")
    text = files["src/Engine/BUILD.bazel"]
    assert '    out = "cswinrt_net8.0-windows10.0.26100.0.cs",' in text
    assert '    out = "cswinrt_net9.0-windows10.0.26100.0.cs",' in text


def test_a_test_project_that_is_a_program_is_a_bazel_test():
    with tempfile.TemporaryDirectory() as reference:
        obj = os.path.join(reference, "src", "Engine.Tests", "obj")
        os.makedirs(obj)
        with open(os.path.join(obj, "TestPlatformEntryPoint.cs"), "w", encoding="utf-8") as f:
            f.write("class EntryPoint { static int Main() => 0; }\n")
        # Microsoft.Testing.Platform writes the project a real entry point.
        own = _digest("Engine.Tests", target="exe", options=["/target:exe"], sources={
            "hand_written": ["src/Engine.Tests/A.cs"],
            "generated": [{"kind": "test_entry_point",
                           "path": "src/Engine.Tests/obj/TestPlatformEntryPoint.cs"}]})
        # The VSTest SDK ships one that does nothing; a host loads the assembly.
        hosted = _digest("Hosted.Tests", target="exe", options=["/target:exe"], sources={
            "hand_written": ["src/Hosted.Tests/A.cs"],
            "external": [{"kind": "nuget", "id": "microsoft.net.test.sdk", "version": "17.14.1",
                          "path": "build/net8.0/Microsoft.NET.Test.Sdk.Program.cs"}]})
        report = _report(own, hosted, _digest("Tool", target="exe", options=["/target:exe"]))
        for name in ("Engine.Tests", "Hosted.Tests"):
            report["targets"][name]["role"] = "test"
        emitter, files = _emit(report, reference_root=reference)
    assert emitter.summary["Engine.Tests"]["rule"] == "csharp_test"
    assert 'load("@rules_dotnet//dotnet:defs.bzl", "csharp_test")' in files["src/Engine.Tests/BUILD.bazel"]
    # As a csharp_test this one would pass without running a test.
    assert emitter.summary["Hosted.Tests"]["rule"] == "csharp_binary"
    assert "VSTest-hosted" in emitter.summary["Hosted.Tests"]["notes"][0]
    assert emitter.summary["Tool"]["rule"] == "csharp_binary"


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
