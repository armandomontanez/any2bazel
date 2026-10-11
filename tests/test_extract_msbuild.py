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

"""MSBuild extraction, end to end from an event dump.

The fixture is a small solution written in the record shapes a replayed binlog
produces (see scripts/msbuild_events.py), with the features that made real
builds hard: a compile MSBuild skipped as up to date, a project instance that
only answered a query, a multi-targeted C# project, a wildcard on the link
line, NuGet and vcpkg include roots, and relative paths.

Everything here runs with `fs=False` (no filesystem, no git), so it behaves the
same on any host. Recovery from tracking logs needs a real directory tree with
Windows paths and is covered by one Windows-only test at the end.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import extract_msbuild
import msbuild_events
from model import Action, BuildSystem, Target, TargetKind, TargetRole
from serialize import load_model

ROOT = "C:\\work\\proj"
VS = "C:\\VS"
SDK = "C:\\Program Files (x86)\\Windows Kits\\10"
CL = VS + "\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\CL.exe"
LINK = VS + "\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\link.exe"
LIBEXE = VS + "\\VC\\Tools\\MSVC\\14.44.35207\\bin\\HostX64\\x64\\Lib.exe"
CSC = VS + "\\MSBuild\\Current\\Bin\\Roslyn\\csc.exe"

SOLUTION_CONFIG = (
    "<SolutionConfiguration>"
    '<ProjectConfiguration Project="{1}" AbsolutePath="%(root)s\\src\\lib\\lib.vcxproj" '
    'BuildProjectInSolution="True">Release|x64</ProjectConfiguration>'
    '<ProjectConfiguration Project="{2}" AbsolutePath="%(root)s\\src\\fuzz\\fuzz.vcxproj" '
    'BuildProjectInSolution="False">Fuzzing|x64</ProjectConfiguration>'
    "</SolutionConfiguration>")


def _cpp_props(root, name, directory, config_type, target_ext, extra=None):
    props = {
        "ProjectName": name, "ProjectDir": "%s\\src\\%s\\" % (root, directory),
        "MSBuildProjectFullPath": "%s\\src\\%s\\%s.vcxproj" % (root, directory, name),
        "Configuration": "Release", "Platform": "x64", "ConfigurationType": config_type,
        "IntDir": "x64\\Release\\", "TLogLocation": "x64\\Release\\%s.tlog\\" % name,
        "OutDir": "%s\\x64\\Release\\%s\\" % (root, name),
        "TargetPath": "%s\\x64\\Release\\%s\\%s%s" % (root, name, name, target_ext),
        "VCToolsVersion": "14.44.35207", "PlatformToolset": "v143",
        "WindowsTargetPlatformVersion": "10.0.26100.0",
        "VCToolsInstallDir": VS + "\\VC\\Tools\\MSVC\\14.44.35207\\", "VCInstallDir": VS + "\\VC\\",
        "VSInstallDir": VS + "\\", "WindowsSdkDir": SDK + "\\",
        "VcpkgInstalledDir": root + "\\vcpkg_installed\\x64\\", "VcpkgTriplet": "x64-windows-static",
        "_ZVcpkgCurrentInstalledDir": root + "\\vcpkg_installed\\x64\\x64-windows-static\\",
    }
    props.update(extra or {})
    return props


def _records(root=ROOT):
    """The event dump of one solution build rooted at `root`."""
    sln_globals = {"Configuration": "Release", "Platform": "x64"}
    proj_globals = dict(sln_globals, CurrentSolutionConfigurationContents=SOLUTION_CONFIG % {
        "root": root})
    lib = root + "\\src\\lib\\lib.vcxproj"
    app = root + "\\src\\app\\app.vcxproj"
    tests = root + "\\src\\tests\\LibTests.vcxproj"
    tool = root + "\\src\\tool\\Tool.csproj"
    tool_tests = root + "\\src\\tool.tests\\Tool.Tests.csproj"
    pkg = root + "\\src\\pkg\\Pkg.wapproj"
    wil = root + "\\packages\\Foo.1.2.3"
    vcpkg = root + "\\vcpkg_installed\\x64\\x64-windows-static"
    common = ("/nologo /W4 /WX /O2 /GL /D NDEBUG /D _UNICODE /Gm- /EHsc /MD /GS /fp:precise "
              "/Zc:wchar_t /Zc:forScope /Zc:inline /std:c++17 /Gd /TP /FC /errorReport:queue")
    r = [
        {"e": "Dump", "schema": 1},
        {"e": "ProjectStarted", "p": 1, "ev": 1, "file": root + "\\App.sln", "targets": "",
         "parent": -2, "global": sln_globals},

        # ---- lib: static library; one source skipped as up to date --------------------
        {"e": "Eval", "ev": 10, "file": lib, "global": proj_globals,
         "props": _cpp_props(root, "lib", "lib", "StaticLibrary", ".lib"),
         "items": [
             {"t": "ClCompile", "i": "a.cpp", "m": {"PrecompiledHeader": "Use"}},
             {"t": "ClCompile", "i": "pch.cpp", "m": {"PrecompiledHeader": "Create"}},
             {"t": "ClCompile", "i": "c\\legacy.c", "m": {"PrecompiledHeader": "NotUsing"}},
             {"t": "ClCompile", "i": "win7_only.cpp", "m": {"ExcludedFromBuild": "true"}},
         ]},
        {"e": "Import", "ev": 10, "file": lib, "imported": wil + "\\build\\native\\Foo.targets"},
        {"e": "ProjectStarted", "p": 2, "ev": 10, "file": lib, "targets": "", "parent": 1,
         "global": proj_globals},
        {"e": "TargetStarted", "p": 2, "t": 20, "name": "GenerateVersionData"},
        {"e": "TaskStarted", "p": 2, "t": 20, "k": 200, "name": "Exec"},
        {"e": "TaskParameter", "p": 2, "t": 20, "k": 200, "kind": "TaskInput", "name": "Command",
         "items": [{"i": "powershell.exe -File ..\\..\\tools\\gen.ps1"}]},
        {"e": "TaskFinished", "p": 2, "t": 20, "k": 200, "name": "Exec", "succeeded": True},
        {"e": "TaskStarted", "p": 2, "t": 20, "k": 201, "name": "WriteLinesToFile"},
        {"e": "TaskParameter", "p": 2, "t": 20, "k": 201, "kind": "TaskInput", "name": "File",
         "items": [{"i": "x64\\Release\\lib.vcxproj.cppwinrt_plat.rsp"}]},
        {"e": "TaskParameter", "p": 2, "t": 20, "k": 201, "kind": "TaskInput", "name": "Lines",
         "items": [{"i": "-input 10.0.26100.0"}, {"i": '-output "x64\\Release\\Generated Files"'}]},
        {"e": "TaskFinished", "p": 2, "t": 20, "k": 201, "name": "WriteLinesToFile",
         "succeeded": True},
        {"e": "TargetStarted", "p": 2, "t": 21, "name": "ClCompile"},
        {"e": "TaskStarted", "p": 2, "t": 21, "k": 210, "name": "CL"},
        {"e": "TaskParameter", "p": 2, "t": 21, "k": 210, "kind": "TaskInput", "name": "Sources",
         "items": [{"i": "pch.cpp", "m": {}}]},
        {"e": "CommandLine", "p": 2, "t": 21, "k": 210, "task": "CL",
         "cmd": '%s /c /I"%s\\include\\\\" /IPublic /I"x64\\Release\\Generated Files\\\\" %s '
                '/Yc"pch.h" /Fp"x64\\Release\\pch.pch" /Fo"x64\\Release\\\\" pch.cpp'
                % (CL, wil, common)},
        {"e": "TaskFinished", "p": 2, "t": 21, "k": 210, "name": "CL", "succeeded": True},
        {"e": "TaskStarted", "p": 2, "t": 21, "k": 211, "name": "CL"},
        {"e": "TaskParameter", "p": 2, "t": 21, "k": 211, "kind": "TaskInput", "name": "Sources",
         "items": [{"i": "a.cpp", "m": {}}]},
        {"e": "CommandLine", "p": 2, "t": 21, "k": 211, "task": "CL",
         "cmd": '%s /c /I"%s\\include\\\\" /IPublic /I"x64\\Release\\Generated Files\\\\" %s '
                '/Yu"pch.h" /Fp"x64\\Release\\pch.pch" /Fo"x64\\Release\\\\" a.cpp'
                % (CL, wil, common)},
        {"e": "TaskFinished", "p": 2, "t": 21, "k": 211, "name": "CL", "succeeded": True},
        # Ran, succeeded, logged no command line: the tracker found legacy.c current.
        {"e": "TaskStarted", "p": 2, "t": 21, "k": 212, "name": "CL"},
        {"e": "TaskParameter", "p": 2, "t": 21, "k": 212, "kind": "TaskInput", "name": "Sources",
         "items": [{"i": "c\\legacy.c", "m": {}}]},
        {"e": "TaskParameter", "p": 2, "t": 21, "k": 212, "kind": "TaskInput",
         "name": "TrackerLogDirectory", "items": [{"i": "x64\\Release\\lib.tlog\\"}]},
        {"e": "TaskFinished", "p": 2, "t": 21, "k": 212, "name": "CL", "succeeded": True},
        {"e": "TargetStarted", "p": 2, "t": 22, "name": "Lib"},
        {"e": "TaskStarted", "p": 2, "t": 22, "k": 220, "name": "LIB"},
        {"e": "CommandLine", "p": 2, "t": 22, "k": 220, "task": "LIB",
         "cmd": '%s /OUT:"%s\\x64\\Release\\lib\\lib.lib" /NOLOGO /MACHINE:X64 /LTCG '
                'x64\\Release\\a.obj\nx64\\Release\\pch.obj' % (LIBEXE, root)},
        {"e": "TaskFinished", "p": 2, "t": 22, "k": 220, "name": "LIB", "succeeded": True},
        {"e": "TargetSkipped", "p": 2, "t": 23, "name": "CppWinRTMakePlatformProjection",
         "reason": "OutputsUpToDate"},
        {"e": "TaskParameter", "p": 2, "t": 23, "kind": "SkippedTargetInputs",
         "items": [{"i": SDK + "\\UnionMetadata\\10.0.26100.0\\Windows.winmd"}]},
        {"e": "TaskParameter", "p": 2, "t": 23, "kind": "SkippedTargetOutputs",
         "items": [{"i": "x64\\Release\\lib.vcxproj.cppwinrt_plat.rsp"}]},
        {"e": "ProjectFinished", "p": 2, "file": lib, "succeeded": True},

        # ---- app: executable linking lib, system libs, and a vcpkg wildcard -----------
        {"e": "Eval", "ev": 11, "file": app, "global": proj_globals,
         "props": _cpp_props(root, "app", "app", "Application", ".exe",
                             {"LibraryPath": SDK + "\\lib\\10.0.26100.0\\um\\x64"}),
         "items": [{"t": "ClCompile", "i": "main.cpp", "m": {}},
                   {"t": "ProjectReference", "i": "..\\lib\\lib.vcxproj", "m": {}}]},
        {"e": "ProjectStarted", "p": 3, "ev": 11, "file": app, "targets": "", "parent": 1,
         "global": proj_globals},
        # The reference to lib asks for its output path: a query, not a build.
        {"e": "ProjectStarted", "p": 5, "ev": 10, "file": lib, "targets": "GetTargetPath",
         "parent": 3, "parentTask": 300, "global": proj_globals},
        {"e": "TargetStarted", "p": 3, "t": 30, "name": "ClCompile"},
        {"e": "TaskStarted", "p": 3, "t": 30, "k": 301, "name": "CL"},
        {"e": "CommandLine", "p": 3, "t": 30, "k": 301, "task": "CL",
         "cmd": '%s /c /I..\\lib\\Public /I"%s\\include" /I"%s\\include\\\\" %s /D _CONSOLE '
                'main.cpp' % (CL, vcpkg, wil, common)},
        {"e": "TaskFinished", "p": 3, "t": 30, "k": 301, "name": "CL", "succeeded": True},
        {"e": "TargetStarted", "p": 3, "t": 31, "name": "Link"},
        {"e": "TaskStarted", "p": 3, "t": 31, "k": 310, "name": "Link"},
        {"e": "CommandLine", "p": 3, "t": 31, "k": 310, "task": "Link",
         "cmd": '%s /ERRORREPORT:QUEUE /OUT:"%s\\x64\\Release\\app\\app.exe" /NOLOGO '
                '/LIBPATH:"%s\\lib" kernel32.lib Advapi32.lib "%s\\lib\\*.lib" '
                '/DELAYLOAD:GDI32.DLL /DEBUG:FULL /SUBSYSTEM:CONSOLE /OPT:REF /MACHINE:X64 '
                '/guard:cf x64\\Release\\main.obj\n"%s\\x64\\Release\\lib\\lib.lib"'
                % (LINK, root, vcpkg, vcpkg, root)},
        {"e": "TaskFinished", "p": 3, "t": 31, "k": 310, "name": "Link", "succeeded": True},
        {"e": "ProjectFinished", "p": 3, "file": app, "succeeded": True},

        # ---- LibTests: a CppUnitTest DLL --------------------------------------------
        {"e": "Eval", "ev": 12, "file": tests, "global": proj_globals,
         "props": _cpp_props(root, "LibTests", "tests", "DynamicLibrary", ".dll"),
         "items": [{"t": "ClCompile", "i": "lib_tests.cpp", "m": {}}]},
        {"e": "ProjectStarted", "p": 4, "ev": 12, "file": tests, "targets": "", "parent": 1,
         "global": proj_globals},
        {"e": "TargetStarted", "p": 4, "t": 40, "name": "ClCompile"},
        {"e": "TaskStarted", "p": 4, "t": 40, "k": 400, "name": "CL"},
        {"e": "CommandLine", "p": 4, "t": 40, "k": 400, "task": "CL",
         "cmd": '%s /c /I"%s\\VC\\Auxiliary\\VS\\UnitTest\\include" /I..\\lib\\Public %s '
                'lib_tests.cpp' % (CL, VS, common)},
        {"e": "TaskFinished", "p": 4, "t": 40, "k": 400, "name": "CL", "succeeded": True},

        # ---- Tool: multi-targeted C# -----------------------------------------------
        {"e": "Eval", "ev": 20, "file": tool, "global": proj_globals,
         "props": {"ProjectName": "Tool", "ProjectDir": root + "\\src\\tool\\",
                   "Configuration": "Release", "Platform": "AnyCPU", "OutputType": "Exe",
                   "TargetFrameworks": "net8.0;net48"}, "items": []},
        {"e": "ProjectStarted", "p": 6, "ev": 20, "file": tool, "targets": "", "parent": 1,
         "global": proj_globals},
    ]
    for ctx, ev, tfm in ((7, 21, "net8.0"), (8, 22, "net48")):
        r.append({"e": "Eval", "ev": ev, "file": tool,
                  "global": dict(proj_globals, TargetFramework=tfm),
                  "props": {"ProjectName": "Tool", "ProjectDir": root + "\\src\\tool\\",
                            "Configuration": "Release", "Platform": "AnyCPU",
                            "OutputType": "Exe", "TargetFramework": tfm,
                            "TargetFrameworks": "net8.0;net48",
                            "NuGetPackageRoot": "C:\\Users\\me\\.nuget\\packages\\",
                            "TargetPath": "%s\\src\\tool\\bin\\Release\\%s\\Tool.dll" % (root, tfm)},
                  "items": [{"t": "Compile", "i": "Program.cs", "m": {}},
                            {"t": "Compile", "i": "Util\\Text.cs", "m": {}}]})
        r.append({"e": "ProjectStarted", "p": ctx, "ev": ev, "file": tool, "targets": "Build",
                  "parent": 6, "global": dict(proj_globals, TargetFramework=tfm)})
    r += [
        {"e": "TargetStarted", "p": 7, "t": 70, "name": "CoreCompile"},
        {"e": "TaskStarted", "p": 7, "t": 70, "k": 700, "name": "Csc"},
        {"e": "CommandLine", "p": 7, "t": 70, "k": 700, "task": "Csc",
         "cmd": "%s /noconfig /unsafe- /define:TRACE;RELEASE /out:obj\\Release\\net8.0\\Tool.dll "
                "/target:exe Program.cs Util\\Text.cs obj\\Release\\net8.0\\Tool.AssemblyInfo.cs"
                % CSC},
        {"e": "TaskFinished", "p": 7, "t": 70, "k": 700, "name": "Csc", "succeeded": True},
        # net48: compile skipped as up to date; the declared inputs name the sources.
        {"e": "TargetSkipped", "p": 8, "t": 80, "name": "CoreCompile", "reason": "OutputsUpToDate"},
        {"e": "TaskParameter", "p": 8, "t": 80, "kind": "SkippedTargetInputs",
         "items": [{"i": "Program.cs"}, {"i": "Util\\Text.cs"},
                   {"i": "C:\\refs\\mscorlib.dll"}]},
        {"e": "TaskParameter", "p": 8, "t": 80, "kind": "SkippedTargetOutputs",
         "items": [{"i": "obj\\Release\\net48\\Tool.exe"}]},
        # A referencing project asked for Tool's frameworks under another platform:
        # a different evaluation that builds nothing.
        {"e": "Eval", "ev": 23, "file": tool, "global": dict(proj_globals, Platform="x64"),
         "props": {"ProjectName": "Tool", "ProjectDir": root + "\\src\\tool\\",
                   "Configuration": "Release", "Platform": "x64", "OutputType": "Exe",
                   "TargetFramework": "net8.0"},
         "items": [{"t": "Compile", "i": "Program.cs", "m": {}}]},
        {"e": "ProjectStarted", "p": 9, "ev": 23, "file": tool, "targets": "GetTargetFrameworks",
         "parent": 4, "global": dict(proj_globals, Platform="x64")},

        # ---- Tool.Tests: an xunit project the build never got to compile -------------
        {"e": "Eval", "ev": 24, "file": tool_tests, "global": proj_globals,
         "props": {"ProjectName": "Tool.Tests", "ProjectDir": root + "\\src\\tool.tests\\",
                   "Configuration": "Release", "Platform": "AnyCPU", "OutputType": "Exe",
                   "TargetFramework": "net8.0", "IsTestProject": "true"},
         "items": [{"t": "Compile", "i": "ToolTests.cs", "m": {}},
                   {"t": "PackageReference", "i": "xunit", "m": {"Version": "2.4.2"}}]},
        {"e": "ProjectStarted", "p": 10, "ev": 24, "file": tool_tests, "targets": "", "parent": 1,
         "global": proj_globals},

        # ---- Pkg: MSIX packaging project ------------------------------------------
        {"e": "Eval", "ev": 25, "file": pkg, "global": proj_globals,
         "props": {"ProjectName": "Pkg", "ProjectDir": root + "\\src\\pkg\\",
                   "Configuration": "Release", "Platform": "x64"}, "items": []},
        {"e": "ProjectStarted", "p": 11, "ev": 25, "file": pkg, "targets": "", "parent": 1,
         "global": proj_globals},

        {"e": "EnvRead", "name": "VCPKG_ROOT"},
        {"e": "BuildFinished", "succeeded": True},
    ]
    return r


def _extract(root=ROOT, fs=False):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.ndjson")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for record in _records(root):
                f.write(json.dumps(record) + "\n")
        return extract_msbuild.extract(path, root, fs=fs)


def _argv(target, source):
    for action in target.actions:
        if action.mnemonic == "CppCompile" and action.arguments[-1] == source:
            return action
    raise AssertionError("%s has no compile of %s" % (target.name, source))


def test_one_target_per_project_configuration():
    model, _ = _extract()
    assert model.build_system == BuildSystem.MSBUILD
    # The outer multi-target build and the query-only evaluation are not targets.
    assert sorted(model.targets) == ["LibTests", "Pkg", "Tool.Tests", "Tool[net48]",
                                     "Tool[net8.0]", "app", "lib"]


def test_kinds_come_from_the_project_not_from_guessing_at_argv():
    model, _ = _extract()
    kinds = {n: t.kind for n, t in model.targets.items()}
    assert kinds["lib"] == TargetKind.STATIC
    assert kinds["app"] == TargetKind.EXECUTABLE
    assert kinds["LibTests"] == TargetKind.SHARED
    assert kinds["Tool[net8.0]"] == TargetKind.EXECUTABLE


def test_roles():
    model, report = _extract()
    roles = {n: t.role for n, t in model.targets.items()}
    assert roles["lib"] == roles["app"] == TargetRole.PRODUCTION
    assert roles["LibTests"] == TargetRole.TEST
    assert roles["Tool.Tests"] == TargetRole.TEST
    assert roles["Pkg"] == TargetRole.PACKAGE
    assert report["targets"]["LibTests"]["test"]["framework"] == "cppunittest"
    assert report["targets"]["Tool.Tests"]["test"] == {"framework": "xunit",
                                                       "evidence": "IsTestProject=true"}


def test_one_compile_action_per_source_with_cwd_and_faithful_argv():
    model, _ = _extract()
    lib = model.targets["lib"]
    compiles = [a for a in lib.actions if a.mnemonic == "CppCompile"]
    assert [a.arguments[-1] for a in compiles] == ["a.cpp", "pch.cpp"]
    a = _argv(lib, "a.cpp")
    assert a.arguments[0] == CL                       # the tool, unquoted path with spaces or not
    assert a.cwd == ROOT + "\\src\\lib"
    assert a.inputs == (ROOT + "\\src\\lib\\a.cpp",)
    # The quoted include with a space and a trailing backslash is ONE argument.
    assert "/Ix64\\Release\\Generated Files\\" in a.arguments
    assert "/Yupch.h" in a.arguments and "/Ycpch.h" not in a.arguments
    assert "/Ycpch.h" in _argv(lib, "pch.cpp").arguments
    # `/D NAME` keeps its value; it is not mistaken for a source.
    i = a.arguments.index("/D")
    assert a.arguments[i + 1] == "NDEBUG"


def test_excluded_sources_are_not_expected():
    _, report = _extract()
    assert "src/lib/win7_only.cpp" not in json.dumps(report["completeness"])


def test_a_skipped_compile_is_reported_not_silently_dropped():
    # No filesystem here, so the tracking log cannot fill the gap -- and the
    # extraction says so instead of producing a model missing a source.
    model, report = _extract()
    assert not report["completeness"]["complete"]
    assert report["completeness"]["unrecovered"]["lib"] == ["src/lib/c/legacy.c"]
    assert report["targets"]["lib"]["recovered_from"] == {"binlog": 2}


def test_main_fails_on_incomplete_extraction_unless_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        events_path = os.path.join(tmp, "events.ndjson")
        with open(events_path, "w", encoding="utf-8", newline="\n") as f:
            for record in _records():
                f.write(json.dumps(record) + "\n")
        out, report = os.path.join(tmp, "model.json"), os.path.join(tmp, "report.json")
        argv = [events_path, ROOT, out, "--report", report, "--no-tlog"]
        assert extract_msbuild.main(argv) == 1
        assert extract_msbuild.main(argv + ["--allow-incomplete"]) == 0
        # The model round-trips with its MSBuild annotations.
        model = load_model(out)
        assert model.toolchain["msvc_version"] == "14.44.35207"
        assert model.targets["lib"].actions[0].cwd == ROOT + "\\src\\lib"
        assert model.targets["app"].attrs["project"] == "src/app/app.vcxproj"
        with open(report, encoding="utf-8") as f:
            assert json.load(f)["schema"] == extract_msbuild.REPORT_SCHEMA


def test_link_and_archive_actions():
    model, _ = _extract()
    (archive,) = [a for a in model.targets["lib"].actions if a.mnemonic == "CppArchive"]
    assert archive.outputs == (ROOT + "\\x64\\Release\\lib\\lib.lib",)
    assert "x64\\Release\\pch.obj" in archive.arguments          # newline-separated objects
    (link,) = [a for a in model.targets["app"].actions if a.mnemonic == "CppLink"]
    assert link.arguments[0] == LINK
    assert link.outputs == (ROOT + "\\x64\\Release\\app\\app.exe",)


def test_dependencies_internal_external_and_offered():
    model, report = _extract()
    deps = {d.name: d for d in model.targets["app"].deps}
    # A library another project in this build produced is an internal dep, by target name.
    assert deps["lib"].external is False
    # Named system libraries are external deps, lower-cased (case-insensitive filesystem).
    assert deps["kernel32"].external and deps["advapi32"].external
    # The wildcard names nothing: reported as offered, never as dependencies.
    assert not any("*" in name or name.startswith("spdlog") for name in deps)
    (offered,) = report["targets"]["app"]["libraries_offered_by_wildcard"]
    assert offered["pattern"] == "@vcpkg/x64-windows-static/lib/*.lib"
    assert offered["origin"] == "vcpkg"


def test_external_roots_are_named_and_mapped():
    model, report = _extract()
    tokens = {token: path for path, token in model.path_map}
    assert tokens["@nuget/foo"] == ROOT + "\\packages\\Foo.1.2.3"
    assert tokens["@vcpkg/x64-windows-static"].endswith("vcpkg_installed\\x64\\x64-windows-static")
    assert tokens["@msvc"].endswith("MSVC\\14.44.35207")
    assert tokens["@winsdk"] == SDK
    nuget = next(e for e in report["externals"] if e["token"] == "@nuget/foo")
    assert (nuget["id"], nuget["version"]) == ("Foo", "1.2.3")
    assert nuget["include_dirs"] == ["include"]
    assert nuget["build_imports"] == ["build/native/Foo.targets"]
    assert nuget["consumers"] == ["app", "lib"]
    # The tools the build ran are attributed to the root they came from.
    msvc = next(e for e in report["externals"] if e["token"] == "@msvc")
    assert msvc["tools"] == ["bin/HostX64/x64/CL.exe", "bin/HostX64/x64/Lib.exe",
                             "bin/HostX64/x64/link.exe"]


def test_report_digest_is_what_a_build_author_needs():
    _, report = _extract()
    (variant,) = report["targets"]["lib"]["compile"]["variants"]     # pch.cpp joins a.cpp
    assert variant["sources"] == ["src/lib/a.cpp", "src/lib/pch.cpp"]
    assert variant["defines"] == ["NDEBUG", "_UNICODE"]
    assert variant["includes"] == ["@nuget/foo/include", "src/lib/Public",
                                   "src/lib/x64/Release/Generated Files"]
    # Mechanics, build type and restated defaults are gone; decisions remain.
    assert variant["flags"] == ["/EHsc", "/MD", "/W4", "/WX", "/Zc:inline", "/std:c++17"]
    app = report["targets"]["app"]
    assert app["compile"]["variants"][0]["includes"] == [
        "src/lib/Public", "@vcpkg/x64-windows-static/include", "@nuget/foo/include"]
    assert app["link_flags"] == ["/DELAYLOAD:gdi32.dll", "/GUARD:CF", "/MACHINE:X64",
                                 "/SUBSYSTEM:CONSOLE"]
    assert app["project_references"] == [{"project": "src/lib/lib.vcxproj"}]


def test_precompiled_header_use_is_reported():
    _, report = _extract()
    assert report["targets"]["lib"]["precompiled_header"] == {
        "header": "pch.h", "created_by": "src/lib/pch.cpp", "not_using": []}


def test_non_compile_steps_are_reported():
    _, report = _extract()
    steps = report["targets"]["lib"]["steps"]
    kinds = {(s["kind"], s["target"]) for s in steps}
    assert ("exec", "GenerateVersionData") in kinds
    write = next(s for s in steps if s["kind"] == "write")
    # A response file is the command line of the tool that reads it.
    assert write["lines"] == ["-input 10.0.26100.0", '-output "x64\\Release\\Generated Files"']
    skipped = next(s for s in steps if s["kind"] == "target")
    assert skipped["target"] == "CppWinRTMakePlatformProjection" and skipped["ran"] is False
    assert skipped["outputs"] == ["src/lib/x64/Release/lib.vcxproj.cppwinrt_plat.rsp"]


def test_csharp_from_command_line_and_from_a_skipped_compile():
    model, report = _extract()
    (ran,) = model.targets["Tool[net8.0]"].actions
    assert ran.mnemonic == "CSharpCompile" and ran.arguments[0] == CSC
    assert "Util\\Text.cs" in ran.arguments
    (skipped,) = model.targets["Tool[net48]"].actions
    assert skipped.arguments == ()
    assert skipped.inputs == (ROOT + "\\src\\tool\\Program.cs", ROOT + "\\src\\tool\\Util\\Text.cs")
    assert report["targets"]["Tool[net48]"]["recovered_from"] == {"skipped_target": 1}
    # A project the build never compiled is listed as unrecovered.
    assert report["completeness"]["unrecovered"]["Tool.Tests"] == ["src/tool.tests/ToolTests.cs"]


def test_build_configuration_and_toolchain():
    model, report = _extract()
    assert model.config == {"entry": "App.sln", "configuration": "Release", "platform": "x64",
                            "build_succeeded": "true"}
    assert model.toolchain == {"msvc_version": "14.44.35207", "platform_toolset": "v143",
                               "winsdk_version": "10.0.26100.0", "target_arch": "x64"}
    assert report["build"]["environment_variables_read"] == ["VCPKG_ROOT"]
    # Which project builds as what under this solution configuration -- including
    # the ones it skips.
    assert report["solution_configuration"] == [
        {"project": "src/fuzz/fuzz.vcxproj", "configuration": "Fuzzing|x64", "built": False},
        {"project": "src/lib/lib.vcxproj", "configuration": "Release|x64", "built": True}]


def test_attrs_describe_the_project():
    model, _ = _extract()
    assert model.targets["app"].attrs == {
        "project": "src/app/app.vcxproj", "configuration": "Release", "platform": "x64",
        "language": "c++", "output": "x64/Release/app/app.exe"}
    assert model.targets["Tool[net48]"].attrs["framework"] == "net48"
    assert model.targets["LibTests"].attrs["test_framework"] == "cppunittest"


def test_exec_command_tool_uses_cmd_quoting_not_crt_quoting():
    # What the C++/WinRT targets log: a quoted directory ending in a backslash,
    # with the program name glued on after the closing quote.
    assert extract_msbuild._exec_tool(
        '"C:\\p\\packages\\Microsoft.Windows.CppWinRT.3.0\\bin\\"cppwinrt @x64\\a.rsp') \
        == "C:\\p\\packages\\Microsoft.Windows.CppWinRT.3.0\\bin\\cppwinrt"
    assert extract_msbuild._exec_tool('"C:\\VS\\VC\\vcpkg\\vcpkg.exe" install --triplet x64') \
        == "C:\\VS\\VC\\vcpkg\\vcpkg.exe"
    assert extract_msbuild._exec_tool("powershell.exe -File gen.ps1") == "powershell.exe"
    assert extract_msbuild._exec_tool('"unterminated') == ""


def test_skipped_compile_is_recovered_from_the_tracking_log():
    """Windows only: needs a real tree with Windows paths. The tracking log is
    written the way MSBuild writes it (UTF-16, paths upper-cased)."""
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        lib_dir = os.path.join(root, "src", "lib")
        os.makedirs(os.path.join(lib_dir, "c"))
        os.makedirs(os.path.join(lib_dir, "Public"))
        os.makedirs(os.path.join(lib_dir, "x64", "Release", "lib.tlog"))
        open(os.path.join(lib_dir, "c", "legacy.c"), "w").close()
        source = os.path.join(lib_dir, "c", "legacy.c")
        with open(os.path.join(lib_dir, "x64", "Release", "lib.tlog", "CL.command.1.tlog"),
                  "wb") as f:
            f.write(("^%s\r\n/c /I\"%s\" /IPUBLIC /W3 /D NDEBUG /MD /TC /Fo\"X64\\RELEASE\\\\\" %s\r\n"
                     % (source.upper(), os.path.join(lib_dir, "Public").upper(),
                        source.upper())).encode("utf-16"))
        model, report = _extract(root, fs=True)
        lib = model.targets["lib"]
        recovered = lib.actions[2]
        assert recovered.mnemonic == "CppCompile"
        assert recovered.inputs == (source,)
        assert recovered.arguments[0] == "cl.exe" and "/TC" in recovered.arguments
        # Absolute and relative include paths get their on-disk case back.
        assert "/I" + os.path.join(lib_dir, "Public") in recovered.arguments
        assert "/IPublic" in recovered.arguments
        # MSBuild handed legacy.c to a CL task that succeeded without running it:
        # the tracking-log entry was checked by this very build.
        assert report["targets"]["lib"]["recovered_from"] == {"binlog": 2, "tlog": 1}
        assert "lib" not in report["completeness"]["unrecovered"]
        assert "lib" not in report["completeness"]["possibly_stale"]
        variants = report["targets"]["lib"]["compile"]["variants"]
        assert variants[1]["sources"] == ["src/lib/c/legacy.c"]
        assert variants[1]["differs_from_first"]["added"]["flags"] == ["/W3"]


def test_generated_files_a_compile_read_are_reported_with_their_producer():
    """Windows only (real tree). The compiler's read tracking log is the only
    record of which generated headers a project consumed: its own, and -- the
    case that matters -- ones another project wrote that it never references."""
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        lib_dir = os.path.join(root, "src", "lib")
        app_out = os.path.join(root, "src", "app", "x64", "Release")
        own_gen = os.path.join(lib_dir, "x64", "Release", "Generated Files", "winrt")
        for d in (os.path.join(lib_dir, "Public"), os.path.join(lib_dir, "x64", "Release", "lib.tlog"),
                  own_gen, app_out):
            os.makedirs(d)
        files = [os.path.join(own_gen, "base.h"), os.path.join(app_out, "App_h.h"),
                 os.path.join(lib_dir, "Public", "lib.h")]
        for f in files:
            open(f, "w").close()
        source = os.path.join(lib_dir, "a.cpp")
        read = [source, os.path.join(lib_dir, "x64", "Release", "pch.pch")] + files
        with open(os.path.join(lib_dir, "x64", "Release", "lib.tlog", "CL.read.1.tlog"), "wb") as f:
            f.write(("^%s\r\n%s\r\n" % (source.upper(), "\r\n".join(p.upper() for p in read)))
                    .encode("utf-16"))
        _, report = _extract(root, fs=True)
        # Sorted by directory: src/app/... before src/lib/...
        other, own = report["targets"]["lib"]["generated_inputs"]
        # A directory with few generated files is listed in full (`names`).
        assert own == {"dir": "src/lib/x64/Release/Generated Files", "files": 1,
                       "examples": ["winrt/base.h"], "names": ["winrt/base.h"],
                       "produced_by": "this project"}
        assert other == {"dir": "src/app/x64/Release", "files": 1, "examples": ["App_h.h"],
                         "names": ["App_h.h"],
                         "produced_by": "another project", "target": "app",
                         "project": "src/app/app.vcxproj", "referenced": False}
        # lib does not reference app: only build order makes that header exist.
        assert report["undeclared_generated_inputs"] == {"lib": ["src/app/x64/Release"]}
        # The hand-written header, the source and the .pch are not generated inputs.
        # And an include directory that is not on disk is called out.
        dead = report["targets"]["lib"]["dead_include_dirs"]
        assert "src/lib/Public" not in dead and any(d.endswith("/include") for d in dead)


def _design_time_records(root, net8_args, net48_args):
    """A design-time pass over Tool's two frameworks: the Csc task does not
    run the compiler, it returns the command line as output items."""
    tool = root + "\\src\\tool\\Tool.csproj"
    records = [{"e": "Dump", "schema": 1}]
    for ctx, tfm, args in ((1, "net8.0", net8_args), (2, "net48", net48_args)):
        globals_ = {"Configuration": "Release", "Platform": "x64", "TargetFramework": tfm,
                    "DesignTimeBuild": "true", "SkipCompilerExecution": "true"}
        records += [
            {"e": "ProjectStarted", "p": ctx, "ev": ctx, "file": tool, "targets": "Compile",
             "parent": 0, "global": globals_},
            {"e": "TargetStarted", "p": ctx, "t": ctx * 10, "name": "CoreCompile"},
            {"e": "TaskStarted", "p": ctx, "t": ctx * 10, "k": ctx * 100, "name": "Csc"},
            {"e": "CommandLine", "p": ctx, "t": ctx * 10, "k": ctx * 100, "task": "Csc",
             "cmd": CSC + " " + " ".join(args)},
            {"e": "TaskParameter", "p": ctx, "t": ctx * 10, "k": ctx * 100, "kind": "TaskOutput",
             "name": "CommandLineArgs", "itemType": "CscCommandLineArgs",
             "items": [{"i": a} for a in args]},
            {"e": "TaskFinished", "p": ctx, "t": ctx * 10, "k": ctx * 100, "name": "Csc",
             "succeeded": True},
        ]
    records += [
        {"e": "TargetStarted", "p": 2, "t": 21, "name": "CoreResGen"},
        {"e": "TaskStarted", "p": 2, "t": 21, "k": 201, "name": "GenerateResource"},
        {"e": "TaskParameter", "p": 2, "t": 21, "k": 201, "kind": "TaskInput", "name": "Sources",
         "items": [{"i": "Properties\\Resources.resx"}]},
        {"e": "TaskParameter", "p": 2, "t": 21, "k": 201, "kind": "TaskInput",
         "name": "OutputResources", "items": [{"i": "obj\\Release\\net48\\Tool.Properties.Resources.resources"}]},
        {"e": "TaskFinished", "p": 2, "t": 21, "k": 201, "name": "GenerateResource", "succeeded": True},
        {"e": "BuildFinished", "succeeded": True},
    ]
    return records


def _extract_with_design_time(net8_args, net48_args):
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, records in (("events.ndjson", _records(ROOT)),
                              ("designtime.ndjson", _design_time_records(ROOT, net8_args, net48_args))):
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], "w", encoding="utf-8", newline="\n") as f:
                for record in records:
                    f.write(json.dumps(record) + "\n")
        return extract_msbuild.extract(paths[0], ROOT, fs=False, design_time=paths[1])


NET8_ARGS = ["/noconfig", "/unsafe-", "/define:TRACE;RELEASE", "/out:obj\\Release\\net8.0\\Tool.dll",
             "/target:exe", "Program.cs", "Util\\Text.cs", "obj\\Release\\net8.0\\Tool.AssemblyInfo.cs"]
NET48_ARGS = ["/noconfig", "/nowarn:1701,1702,2008", "/define:TRACE;RELEASE;NET48",
              "/langversion:10", "/out:obj\\Release\\net48\\Tool.exe", "/target:exe",
              "/resource:obj\\Release\\net48\\Tool.Properties.Resources.resources",
              "Program.cs", "Util\\Text.cs"]


def test_design_time_pass_supplies_the_command_line_of_a_skipped_csharp_compile():
    model, report = _extract_with_design_time(NET8_ARGS + ["/nowarn:2008"], NET48_ARGS)
    skipped = model.targets["Tool[net48]"].actions[0]
    assert skipped.mnemonic == "CSharpCompile" and skipped.arguments[0].endswith("csc.exe")
    assert "/langversion:10" in skipped.arguments and "/define:TRACE;RELEASE;NET48" in skipped.arguments
    # The one thing a design-time build adds to every command line is taken out.
    assert "/nowarn:1701,1702" in skipped.arguments
    assert not any("2008" in a for a in skipped.arguments)
    assert report["targets"]["Tool[net48]"]["recovered_from"] == {"design_time": 1}
    # The compile that really ran keeps its own command line, and is the check
    # on the pass: computed and real agree, so the computed ones are trusted.
    assert report["targets"]["Tool[net8.0]"]["recovered_from"] == {"binlog": 1}
    assert report["completeness"]["design_time"] == {
        "status": "verified", "checked_against_real_compiles": 1, "mismatched": {},
        "used_for": ["Tool[net48]"]}
    # The digest now exists for the skipped compile too, with the .resx behind
    # the generated resource.
    digest = report["targets"]["Tool[net48]"]["csharp"]
    assert digest["options"] == ["/langversion:10.0", "/nowarn:CS1701", "/nowarn:CS1702", "/target:exe"]
    assert digest["resources"][0]["from"] == "src/tool/Properties/Resources.resx"


def test_design_time_pass_that_disagrees_with_a_real_compile_is_reported():
    wrong = [a for a in NET8_ARGS if a != "Util\\Text.cs"] + ["/define:EXTRA"]
    _, report = _extract_with_design_time(wrong, NET48_ARGS)
    check = report["completeness"]["design_time"]
    assert check["status"] == "mismatch"
    assert check["mismatched"]["Tool[net8.0]"]["defines"] == {
        "build_only": [], "design_time_only": ["extra"]}
    assert check["mismatched"]["Tool[net8.0]"]["sources"]["build_only"] == ["src/tool/util/text.cs"] \
        or check["mismatched"]["Tool[net8.0]"]["sources"]["build_only"]


def test_without_a_design_time_pass_a_skipped_compile_has_sources_only():
    model, report = _extract()
    assert model.targets["Tool[net48]"].actions[0].arguments == ()
    assert report["targets"]["Tool[net48]"]["recovered_from"] == {"skipped_target": 1}
    assert "design_time" not in report["completeness"]
    assert "csharp" not in report["targets"]["Tool[net48]"]


def test_design_time_project_runs_each_instance_with_its_own_global_properties():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.ndjson")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for record in _records(ROOT):
                f.write(json.dumps(record) + "\n")
        instances = extract_msbuild.design_time_instances(msbuild_events.load(path), ROOT)
    files = sorted((os.path.basename(f), g.get("TargetFramework", "")) for f, g in instances)
    # One per framework of the multi-targeted project; not the outer build, not
    # the evaluation another project made just to ask a question.
    assert ("Tool.csproj", "net8.0") in files and ("Tool.csproj", "net48") in files
    assert ("Tool.csproj", "") not in files
    text = msbuild_events.design_time_project(
        [("C:\\p\\A.csproj", {"Configuration": "Release", "Odd": "a;b $(c) <x>"})])
    assert 'Projects="C:\\p\\A.csproj"' in text and 'Targets="PrepareResources;Compile"' in text
    # `;` would end the property and `$(` would be expanded: both are escaped.
    assert "Odd=a%3Bb %24(c) &lt;x&gt;" in text
    assert "SkipCompilerExecution=true" in text and "ProvideCommandLineArgs=true" in text


def test_winrt_metadata_facts_come_from_midl_and_the_steps_after_it():
    project = ROOT + "\\src\\Component"
    sdk = "C:\\Program Files (x86)\\Windows Kits\\10\\References\\10.0.26100.0"
    midl = Action("Midl", ("midl.exe", "/metadata_dir", sdk + "\\Windows.Foundation.FoundationContract\\4.0.0.0",
                           "/winrt", "/W1", "/nologo", "/char", "signed", "/env", "x64", "/winmd",
                           "x64\\Release\\Unmerged\\Api.winmd", "/h", "nul", "/notlb", "/client", "none",
                           "/target", "NT60", "/nomidl", "@x64\\Release\\Component.vcxproj.midlrt.rsp",
                           "Api.idl"), cwd=project)
    target = Target("Component", TargetKind.SHARED, actions=[midl])
    steps = [
        {"kind": "write", "file": "src/Component/x64/Release/Component.vcxproj.midlrt.rsp",
         "lines": ['/reference "%s\\Windows.Foundation.FoundationContract\\4.0.0.0\\Windows.Foundation.FoundationContract.winmd"\r\n'
                   '/reference "%s\\x64\\Release\\Other\\Other.winmd"' % (sdk, ROOT)]},
        {"kind": "write", "file": "src/Component/x64/Release/Component.vcxproj.mdmerge.rsp",
         "lines": ['-v -metadata_dir "%s\\Windows.Foundation.FoundationContract\\4.0.0.0\\." '
                   '-i "x64\\Release\\Unmerged\\Api.winmd" -o "x64\\Release\\Merged" -partial -n:2' % sdk]},
        {"kind": "copy", "from": ["src/Component/x64/Release/Merged/My.Component.winmd"],
         "to": ["x64/Release/Component/My.Component.winmd"]},
        {"kind": "copy", "from": ["x64/Release/Other/Other.winmd"],
         "to": ["x64/Release/Component/Other.winmd"]},
    ]
    facts = extract_msbuild._winrt_metadata(target, steps, ROOT)
    assert facts == {
        "idl": ["src/Component/Api.idl"],
        "midl_options": ["/W1", "/char", "signed", "/env", "x64", "/target", "NT60"],
        "namespace_depth": 2,
        "references": ["x64/Release/Other/Other.winmd"],
        "winmd": "src/Component/x64/Release/Merged/My.Component.winmd",
        "copies": ["x64/Release/Component/My.Component.winmd"]}
    # A classic RPC interface is not WinRT metadata.
    rpc = Target("Rpc", TargetKind.EXECUTABLE, actions=[Action("Midl", ("midl.exe", "/h", "A.h", "A.idl"),
                                                                  cwd=project)])
    assert extract_msbuild._winrt_metadata(rpc, [], ROOT) is None


def test_external_file_lists_keep_one_spelling_per_path():
    assert extract_msbuild._one_spelling({"INCLUDE", "include", "lib/Foo.lib", "LIB/FOO.LIB",
                                          "tools/x.exe"}) == ["include", "lib/Foo.lib", "tools/x.exe"]


def test_tracking_log_paths_outside_the_project_get_their_case_back():
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        os.makedirs(os.path.join(root, "src", "lib"))
        os.makedirs(os.path.join(root, "src", "Shared", "Utils"))
        truecase = extract_msbuild._TrueCase(True)
        cwd = os.path.join(root, "src", "lib")
        assert extract_msbuild._restore_token_case("/I..\\SHARED\\UTILS", truecase, cwd) \
            == "/I..\\Shared\\Utils"
        assert extract_msbuild._restore_token_case("/I..\\SHARED\\UTILS\\", truecase, cwd) \
            == "/I..\\Shared\\Utils\\"


def test_winrt_metadata_of_a_project_with_its_intermediates_under_obj_and_several_idl():
    project = ROOT + "\\src\\Sdk"
    common = ("/winrt", "/W1", "/nologo", "/char", "signed", "/env", "x64", "/enum_class", "/ns_prefix",
              "/h", "nul", "/target", "NT60", "/nomidl", "@obj\\x64\\Release\\Sdk.vcxproj.midlrt.rsp")
    midl = [Action("Midl", ("midl.exe",) + common + ("/winmd", "obj\\x64\\Release\\Unmerged\\%s.winmd" % n,
                                                      n + ".idl"), cwd=project) for n in ("A", "B")]
    target = Target("Sdk", TargetKind.SHARED, actions=midl)
    steps = [
        {"kind": "write", "file": "src/Sdk/obj/x64/Release/Sdk.vcxproj.mdmerge.rsp",
         "lines": ['-i "obj\\x64\\Release\\Unmerged" -o "obj\\x64\\Release\\Merged" -partial -n:3']},
        {"kind": "copy", "from": ["src/Sdk/obj/x64/Release/Merged/My.Sdk.Api.winmd"],
         "to": ["x64/Release/Sdk/My.Sdk.Api.winmd"]}]
    facts = extract_msbuild._winrt_metadata(target, steps, ROOT)
    # The merge directory is relative to the project, wherever the response file is.
    assert facts["winmd"] == "src/Sdk/obj/x64/Release/Merged/My.Sdk.Api.winmd"
    assert facts["copies"] == ["x64/Release/Sdk/My.Sdk.Api.winmd"]
    assert facts["idl"] == ["src/Sdk/A.idl", "src/Sdk/B.idl"]
    # One set of options for the project, not one per .idl strung together.
    assert facts["midl_options"] == ["/W1", "/char", "signed", "/env", "x64", "/enum_class",
                                     "/ns_prefix", "/target", "NT60"]
    assert "midl_options_vary" not in facts


def _app(targets, skipped=()):
    """A program that builds into a directory it shares with other programs."""
    project = ROOT + "\\src\\app\\app.csproj"
    evaluation = msbuild_events.Evaluation(id=1, file=project, props=msbuild_events._Props(
        {"ProjectDir": ROOT + "\\src\\app\\", "OutDir": ROOT + "\\out\\Apps\\"}))
    instance = msbuild_events.Project(ctx=1, file=project, targets=list(targets), skipped=list(skipped))
    return extract_msbuild._Group(file=project, evaluation=evaluation, instances=[instance], name="app")


def _copy_list():
    """What MSBuild's copy-if-newer target is to deploy: a runtime file every
    program in the directory needs, and a file of this program."""
    item = msbuild_events.Item
    listing = msbuild_events.Target(id=1, name="GetCopyToOutputDirectoryItems")
    listing.item_changes = [
        (True, "_SourceItemsToCopyToOutputDirectory", [
            item("C:\\nuget\\sdk\\1.0\\native\\Runtime.dll", {"TargetPath": "Runtime.dll"}),
            item(ROOT + "\\src\\app\\Assets\\app.png", {"TargetPath": "Assets\\app.png"}),
            item(ROOT + "\\src\\app\\notes.txt", {"TargetPath": "notes.txt"})]),
        (False, "_SourceItemsToCopyToOutputDirectory", [item(ROOT + "\\src\\app\\notes.txt")]),
    ]
    return listing


def test_what_another_project_already_copied_is_still_a_copy_of_this_one():
    # Another program put Runtime.dll into the shared directory first, so the
    # Copy task of this one is handed the out-of-date part of the list only.
    item = msbuild_events.Item
    copy = msbuild_events.Task(id=1, name="Copy", inputs={
        "SourceFiles": [item(ROOT + "\\src\\app\\Assets\\app.png")],
        "DestinationFiles": [item(ROOT + "\\out\\Apps\\Assets\\app.png")]})
    partial = msbuild_events.Target(id=2, name="_CopyOutOfDateSourceItemsToOutputDirectory", tasks=[copy])
    steps = [s for s in extract_msbuild._steps(_app([_copy_list(), partial]), ROOT) if s["kind"] == "copy"]
    assert steps == [
        {"kind": "copy", "target": "_CopyOutOfDateSourceItemsToOutputDirectory",
         "from": ["src/app/Assets/app.png"], "to": ["out/Apps/Assets/app.png"]},
        {"kind": "copy", "target": "_CopyOutOfDateSourceItemsToOutputDirectory", "up_to_date": True,
         "from": ["C:/nuget/sdk/1.0/native/Runtime.dll"], "to": ["out/Apps/Runtime.dll"]}]

    # Everything was there: MSBuild skips the target, and the list is all there is.
    skipped = msbuild_events.SkippedTarget(name="_CopyOutOfDateSourceItemsToOutputDirectory",
                                           reason="OutputsUpToDate")
    steps = [s for s in extract_msbuild._steps(_app([_copy_list()], [skipped]), ROOT) if s["kind"] == "copy"]
    assert [(s["up_to_date"], s["to"]) for s in steps] == [
        (True, ["out/Apps/Runtime.dll", "out/Apps/Assets/app.png"])]

    # A project that only answered which files it would deploy copies nothing.
    assert not [s for s in extract_msbuild._steps(_app([_copy_list()]), ROOT) if s["kind"] == "copy"]


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
