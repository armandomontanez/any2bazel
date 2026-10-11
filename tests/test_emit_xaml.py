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

"""emit_dotnet.py for projects that compile XAML: the WinUI and WPF chains, the
resource index, and the directory a program runs from."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import emit_dotnet
from emit_dotnet import Emitter

TFM = "net10.0-windows10.0.26100.0"
WINUI = "microsoft.windowsappsdk.winui/2.2.1"
OBJ = "src/app/obj/Release"
NUGET = "C:/Users/me/.nuget/packages"


def _digest(**over):
    d = {"assembly": "Contoso.App", "target": "winexe", "target_framework": TFM,
         "defines": ["NET", "NET10_0", "WINDOWS"], "options": ["/langversion:14.0", "/target:winexe"],
         "sources": {"hand_written": ["src/app/App.xaml.cs"]},
         "references": {"packs": {}}, "analyzers": {}}
    d.update(over)
    return d


def _winui():
    code = [OBJ + "/App.g.i.cs", OBJ + "/App.g.cs", OBJ + "/Views/Main.g.i.cs",
            OBJ + "/Views/Main.g.cs", OBJ + "/XamlTypeInfo.g.cs"]
    d = _digest(xaml={
        "framework": "winui", "project": "src/app/App.csproj",
        "pass1": {"output_path": OBJ, "settings": {"IsPass1": "True", "RootNamespace": "App"},
                  "applications": [{"path": "src/app/App.xaml", "spec": "App.xaml"}],
                  "pages": [{"path": "src/app/Views/Main.xaml", "spec": "Views\\Main.xaml"}],
                  "reference_order": ["System.Runtime.dll", "Contoso.Lib.dll"],
                  "references_not_from_packages": ["src/lib/bin/Contoso.Lib.dll"],
                  "outputs": {"code": code}},
        "pass2": {"output_path": OBJ, "settings": {"IsPass1": "False", "RootNamespace": "App"},
                  "local_assembly": OBJ + "/intermediatexaml/Contoso.App.dll",
                  "outputs": {"xaml": [OBJ + "/App.xaml", OBJ + "/Views/Main.xaml"],
                              "xbf": [OBJ + "/App.xbf", OBJ + "/Views/Main.xbf"]}}})
    d["sources"]["generated"] = [{"kind": "xaml_or_generator_output", "path": p} for p in code]
    return d


def _lock(**target):
    entry = {"framework": TFM, "packs": {}, "packages": {WINUI: TFM},
             "files": {WINUI: ["tools/net472/XamlCompiler.exe"]}}
    entry.update(target)
    return {"packages": {WINUI: {}}, "targets": {"App": entry}}


def _emit(digest, lock, reference=None, workspace=(), **target):
    """(emitter, the project's BUILD file) with `reference` as the files of the
    reference tree ({path: text}) and `workspace` as source files that exist."""
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ref:
        for base, files in ((ref, reference or {}), (root, {p: "" for p in workspace})):
            for path, text in files.items():
                full = os.path.join(base, path.replace("/", os.sep))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(text)
        entry = dict({"project": "src/app/App.csproj", "csharp": digest,
                      "output": "x64/Release/Contoso.App.dll", "steps": []}, **target)
        emitter = Emitter({"targets": {"App": entry}}, lock, root, reference_root=ref)
        files = emitter.emit()
        return emitter, files.get("src/app/BUILD.bazel", "")


def test_a_winui_project_is_compiled_twice_around_the_xaml_compiler():
    emitter, text = _emit(_winui(), _lock())
    assert emitter.summary["App"]["xaml"] == "winui" and emitter.summary["App"]["exact"]
    # Pass 1: every generated file, under the name of the pass.
    first = text[text.index('name = "App_xaml1"'):text.index("csc_assembly(")]
    assert '    applications = [\n        "App.xaml",\n    ],' in first
    assert '    pages = [\n        "Views/Main.xaml",\n    ],' in first
    assert ('    compiler = "@nuget//microsoft.windowsappsdk.winui/2.2.1:tools/net472/XamlCompiler.exe",'
            in first)
    assert ('    outs = [\n        "App_xaml1/App.g.i.cs",\n        "App_xaml1/App.g.cs",\n'
            '        "App_xaml1/Views/Main.g.i.cs",\n        "App_xaml1/Views/Main.g.cs",\n'
            '        "App_xaml1/XamlTypeInfo.g.cs",\n    ],') in first
    # The compiler is told which references are not a package's, and their order.
    assert '    not_from_packages = [\n        "Contoso.Lib.dll",\n    ],' in first
    assert '    reference_order = [\n        "System.Runtime.dll",\n        "Contoso.Lib.dll",\n    ],' in first
    assert '        "IsPass1": "True",' in first and "local_assembly" not in first
    # The compile in between: the project with ALL of pass 1's code.
    between = text[text.index('name = "App_intermediate"'):text.index('name = "App_xaml2"')]
    for source in ("App.xaml.cs", ":App_xaml1/App.g.cs", ":App_xaml1/App.g.i.cs",
                   ":App_xaml1/XamlTypeInfo.g.cs"):
        assert '"%s"' % source in between, source
    assert '    assembly_name = "Contoso.App",' in between
    # Pass 2 reads that assembly and writes the rest of the code and the markup.
    second = text[text.index('name = "App_xaml2"'):text.index("exact_csharp_assembly(")]
    assert '    local_assembly = ":App_intermediate",' in second
    assert ('    outs = [\n        "App_xaml2/App.g.cs",\n        "App_xaml2/Views/Main.g.cs",\n'
            '        "App_xaml2/XamlTypeInfo.g.cs",\n        "App_xaml2/App.xaml",\n'
            '        "App_xaml2/Views/Main.xaml",\n        "App_xaml2/App.xbf",\n'
            '        "App_xaml2/Views/Main.xbf",\n    ],') in second
    # The project's own compile: the field declarations of pass 1, the rest of pass 2.
    final = text[text.index("exact_csharp_assembly("):]
    for source in (":App_xaml1/App.g.i.cs", ":App_xaml1/Views/Main.g.i.cs", ":App_xaml2/App.g.cs",
                   ":App_xaml2/Views/Main.g.cs", ":App_xaml2/XamlTypeInfo.g.cs"):
        assert '"%s"' % source in final, source
    assert '":App_xaml1/App.g.cs"' not in final
    # The differ is told which generated file is which, and that the compiler's
    # launcher is a tool of the build.
    config = emitter.config_suggestions()
    assert {"from": OBJ + "/Views/Main.g.cs", "to": "@xaml/App/Views/Main.g.cs"} in config["ignore"]["source_map"]
    assert {"from": "bazel-bin/src/app/App_xaml2/Views/Main.g.cs",
            "to": "@xaml/App/Views/Main.g.cs"} in config["ignore"]["source_map"]
    assert emit_dotnet.XAML_TOOL in config["exclude_targets"]


def test_a_winui_project_needs_the_compiler_in_the_lock():
    emitter, text = _emit(_winui(), _lock(files={}))
    assert text == ""
    assert "does not list the XAML compiler's files" in emitter.summary["App"]["unhandled"][0]


def test_the_resource_index_merges_what_the_project_uses():
    pri = {"output": "x64/Release/Contoso.App.pri", "index_name": "Contoso.App",
           "config": OBJ + "/priconfig.xml",
           "makepri": NUGET + "/microsoft.windows.sdk.buildtools/10.0.1/bin/x64/makepri.exe",
           "layout": [{"path": "src/app/Assets/Logo.png", "name": "Assets\\Logo.png"}],
           # MSBuild embeds the copy it made in the output directory.
           "embed": [{"path": "x64/Release/Views/Main.xbf", "name": "Views\\Main.xbf"}],
           "resources": ["src/app/Strings/en-us/Resources.resw"],
           "pri_files": [NUGET + "/some.controls/1.0.0/lib/net8.0/Some.Controls.pri"]}
    emitter, text = _emit(_winui(), _lock(), {OBJ + "/priconfig.xml": "<resources/>\n"}, pri=pri)
    assert emitter.summary["App"]["pri"] == ":App_pri"
    block = text[text.index("winui_pri("):]
    block = block[:block.index("\n)\n") + 2]
    assert '    out = "Contoso.App.pri",' in block and '    index_name = "Contoso.App",' in block
    assert '    config = ":App_priconfig",' in block
    assert '        ":App_xaml2/Views/Main.xbf": "Views\\\\Main.xbf",' in block
    assert '        "Assets/Logo.png": "Assets\\\\Logo.png",' in block
    assert ('    makepri = "@nuget//microsoft.windows.sdk.buildtools/10.0.1:bin/x64/makepri.exe",'
            in block)
    assert '        "@nuget//some.controls/1.0.0:lib/net8.0/Some.Controls.pri",' in block
    assert '        "Strings/en-us/Resources.resw",' in block
    # Without the reference tree the configuration cannot be restated.
    emitter, text = _emit(_winui(), _lock(), pri=pri)
    assert "priconfig.xml is not readable" in emitter.summary["App"]["unhandled"][0]


def test_a_program_gets_its_host_and_its_directory():
    digest = _winui()
    digest["apphost"] = {"template": NUGET + "/microsoft.netcore.app.host.win-x64/10.0.1/runtimes/"
                                             "win-x64/native/apphost.exe",
                         "binary": "Contoso.App.dll", "gui": True}
    steps = [{"kind": "copy", "target": "_CopyFilesMarkedCopyLocal",
              "from": [NUGET + "/some.controls/1.0.0/lib/net8.0/Some.Controls.dll",
                       "src/app/Assets/Logo.png", "src/app/obj/Release/apphost.exe",
                       "src/lib/bin/Mystery.dll", "src/app/obj/Release/Contoso.App.pdb"],
              "to": ["x64/Release/Some.Controls.dll", "x64/Release/Assets/Logo.png",
                     "x64/Release/Contoso.App.exe", "x64/Release/Mystery.dll",
                     "x64/Release/Contoso.App.pdb"]}]
    reference = {"x64/Release/Contoso.App.runtimeconfig.json": "{}\n",
                 "x64/Release/Contoso.App.deps.json": "{}\n"}
    emitter, text = _emit(digest, _lock(), reference, workspace=["src/app/Assets/Logo.png"],
                          steps=steps)
    summary = emitter.summary["App"]
    assert summary["program"] == ":Contoso.App"
    # What no target, package or source file provides is said, not guessed.
    assert summary["not_laid_out"] == ["Mystery.dll"]
    assert any("1 debug-symbol and documentation files" in n for n in summary["notes"])
    assert ('dotnet_apphost(\n    name = "App_host",\n    out = "Contoso.App.exe",\n'
            '    assembly = ":App_csc",\n    gui = True,\n'
            '    template = "@nuget//microsoft.netcore.app.host.win-x64/10.0.1:runtimes/win-x64/'
            'native/apphost.exe",\n)') in text
    directory = text[text.index("program_directory("):]
    assert '    name = "Contoso.App",\n    executable = "Contoso.App.exe",' in directory
    for line in ('        "Assets/Logo.png": "Assets/Logo.png",',
                 '        "Contoso.App.deps.json": ":App_deps",',
                 '        "Contoso.App.dll": ":App_csc",',
                 '        "Contoso.App.exe": ":App_host",',
                 '        "Contoso.App.runtimeconfig.json": ":App_runtimeconfig",',
                 '        "Some.Controls.dll": "@nuget//some.controls/1.0.0:lib/net8.0/Some.Controls.dll",'):
        assert line in directory, line
    assert "Mystery" not in directory and ".pdb" not in directory


def _wpf():
    code = [OBJ + "/App.g.cs", OBJ + "/Main.g.cs"]
    d = _digest(
        resources=[{"name": "Contoso.App.g.resources", "kind": "generated",
                    "path": OBJ + "/Contoso.App.g.resources"}],
        xaml={"framework": "wpf", "project": "src/app/App.csproj",
              "pass1": {"output_path": OBJ, "settings": {"AssemblyName": "Contoso.App", "OutputType": "WinExe"},
                        "applications": [{"path": "src/app/App.xaml", "spec": "App.xaml"}],
                        "pages": [{"path": "src/app/Main.xaml", "spec": "Main.xaml"}],
                        "content": ["Assets\\readme.txt"], "requires_pass2": True,
                        "state": [OBJ + "/Contoso.App_MarkupCompile.cache", OBJ + "/Contoso.App_MarkupCompile.lref"],
                        "outputs": {"code": code, "baml": [OBJ + "/Early.baml"]}},
              "pass2": {"settings": {"AssemblyName": "Contoso.App"},
                        "outputs": {"baml": [OBJ + "/App.baml", OBJ + "/Main.baml"]}},
              "resources": {"output": OBJ + "/Contoso.App.g.resources",
                            "files": [{"path": OBJ + "/Early.baml"}, {"path": OBJ + "/App.baml"},
                                      {"path": OBJ + "/Main.baml"}, {"path": "src/app/images/app.ico"}]}})
    d["sources"]["generated"] = [{"kind": "xaml_or_generator_output", "path": p} for p in code]
    return d


def test_a_wpf_project_compiles_its_markup_in_two_passes_and_embeds_it():
    emitter, text = _emit(_wpf(), _lock(packages={}, files={}))
    assert emitter.summary["App"]["xaml"] == "wpf", emitter.summary["App"]
    first = text[text.index('name = "App_wpf1"'):text.index("csc_assembly(")]
    assert "    pass_ = 1," in first and '    project_dir = "src/app",' in first
    assert '    content = [\n        "Assets\\\\readme.txt",\n    ],' in first
    assert ('    outs = [\n        "App_wpf1/App.g.cs",\n        "App_wpf1/Main.g.cs",\n'
            '        "App_wpf1/Early.baml",\n        "App_wpf1/Contoso.App_MarkupCompile.cache",\n'
            '        "App_wpf1/Contoso.App_MarkupCompile.lref",\n    ],') in first
    assert '        "App.xaml.cs",' in first[first.index("sources = ["):]
    # The temporary assembly, and pass 2 with what pass 1 left for it.
    assert 'csc_assembly(\n    name = "App_intermediate",' in text
    second = text[text.index('name = "App_wpf2"'):text.index("wpf_resources(")]
    assert '    local_assembly = ":App_intermediate",' in second and "    pass_ = 2," in second
    assert ('    state = [\n        ":App_wpf1/Contoso.App_MarkupCompile.cache",\n'
            '        ":App_wpf1/Contoso.App_MarkupCompile.lref",\n    ],') in second
    # Compiled markup of both passes and the project's own resource files, in
    # one resources file, which the assembly embeds under MSBuild's name.
    packed = text[text.index("wpf_resources("):text.index("exact_csharp_assembly(")]
    for line in ('        ":App_wpf1/Early.baml": "Early.baml",', '        ":App_wpf2/App.baml": "App.baml",',
                 '        ":App_wpf2/Main.baml": "Main.baml",', '        "images/app.ico": "images/app.ico",'):
        assert line in packed, line
    assert '    out = "Contoso.App.g.resources",' in packed
    final = text[text.index("exact_csharp_assembly("):]
    assert '        ":App_markup": "Contoso.App.g.resources",' in final
    assert '":App_wpf1/App.g.cs"' in final and '":App_wpf1/Main.g.cs"' in final


def test_the_temporary_project_of_the_wpf_compiler_is_not_a_target():
    with tempfile.TemporaryDirectory() as root:
        report = {"targets": {
            "App": {"project": "src/app/App.csproj", "csharp": _digest(), "steps": []},
            "App_abc123_wpftmp": {"project": "src/app/App_abc123_wpftmp.csproj", "csharp": _digest(),
                                  "steps": [{"kind": "exec", "command": "tool.exe"}]}}}
        emitter = Emitter(report, {"targets": {}}, root)
        emitter.emit()
        assert "temporary project" in emitter.summary["App_abc123_wpftmp"]["excluded"]
        # A generator both projects would run ran there: its steps count for the project.
        assert {"kind": "exec", "command": "tool.exe"} in emitter._steps("App")


def test_another_emitters_exports_survive_a_rewrite():
    existing = (emit_dotnet.MARKER + ".\n\ncsharp_library(name = \"x\")\n\n"
                "# any2bazel:cc-exports begin (generated; do not edit between the markers)\n"
                'exports_files([\n    "a.h",\n])\n# any2bazel:cc-exports end\n')
    kept = emit_dotnet.foreign_export_blocks(existing, emit_dotnet.EXPORTS_BEGIN)
    assert kept.startswith("\n# any2bazel:cc-exports begin") and kept.endswith("# any2bazel:cc-exports end\n")
    own = existing.replace("cc-exports", "dotnet-exports")
    assert emit_dotnet.foreign_export_blocks(own, emit_dotnet.EXPORTS_BEGIN) == ""


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
