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

"""emit_cc.py: report + lock -> rules_cc BUILD files."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import emit_cc
from emit_cc import Emitter

REFERENCE = r"C:\ref"
SDK = r"C:\Program Files (x86)\Windows Kits\10\References\10.0.26100.0"
CPPWINRT = r"C:\ref\packages\Microsoft.Windows.CppWinRT.2.0.1\bin\cppwinrt"


def _target(project, kind="static_library", sources=None, **over):
    directory = project.rsplit("/", 1)[0]
    entry = {
        "project": project, "kind": kind, "configuration": "Release|x64",
        "output": "x64/Release/%s" % over.pop("output", project.rsplit("/", 1)[-1][:-8] + ".lib"),
        "compile": {"variants": [{
            "sources": sources or [directory + "/a.cpp"],
            "defines": ["NDEBUG", "UNICODE"],
            "includes": [directory, "src"],
            "flags": ["/EHsc", "/MT", "/W4", "/permissive", "/std:c++latest"],
        }]},
    }
    entry.update(over)
    return entry


def _externals(*consumers):
    return [{"kind": "nuget", "id": "Microsoft.Windows.CppWinRT", "version": "2.0.1",
             "root": r"C:\ref\packages\Microsoft.Windows.CppWinRT.2.0.1", "tools": ["bin/cppwinrt"],
             "consumers": list(consumers)},
            {"kind": "nuget", "id": "microsoft.windows.implementationlibrary", "version": "1.0.2",
             "root": r"C:\Users\me\.nuget\packages\microsoft.windows.implementationlibrary\1.0.2",
             "include_dirs": ["include"], "consumers": list(consumers)}]


def _emit(targets, lock=None, externals=None, existing=None, **kw):
    """(emitter, files); every repo file exists unless `existing` lists what does."""
    with tempfile.TemporaryDirectory() as root:
        def exists(path):
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            return existing is None or relative in existing
        emitter = Emitter({"targets": targets, "externals": externals or []},
                          lock or {"packages": {}, "targets": {}}, root,
                          reference_root=REFERENCE, file_exists=exists, **kw)
        return emitter, emitter.emit()


def test_a_static_library_restates_its_compile():
    lib = _target("src/lib/lib.vcxproj", headers=["src/lib/a.h", "src/common/util.h"],
                  include_environment=["src/lib/Stray", "packages", "@vcpkg/x64/include"],
                  external_headers={"flags": ["/external:W0"]},
                  optimization={"compile": ["/O2", "/GL"]})
    emitter, files = _emit({"lib": lib})
    text = files["src/lib/BUILD.bazel"]
    assert text.startswith(emit_cc.MARKER)
    assert 'load("@rules_cc//cc:defs.bzl", "cc_library")' in text
    # Sources, then the headers the compiles read: its own by name, another
    # package's by label (and exported from there).
    assert '    srcs = [\n        "a.cpp",\n        "a.h",\n        "//:src/common/util.h",\n    ],' in text
    assert emitter.workspace.exports == {"": {"src/common/util.h"}}
    # Include directories are the project's own business: copts, not `includes`.
    # A directory of the INCLUDE variable inside a project comes last; the
    # folder of restored packages and a package's directory are not passed.
    # `/std:` goes first and `/permissive` next: the report sorts options, and
    # /std:c++latest would otherwise undo a /permissive before it.
    assert ('    copts = [\n        "/Isrc/lib",\n        "/Isrc",\n        "/Isrc/lib/Stray",\n'
            '        "/std:c++latest",\n        "/permissive",\n        "/EHsc",\n        "/W4",\n'
            '        "/external:W0",\n        "/O2",\n        "/GL",\n    ],') in text
    assert '    local_defines = [\n        "NDEBUG",\n        "UNICODE",\n    ],' in text
    # /MT is a feature: it also selects the default library of the link.
    assert '    features = [\n        "static_link_msvcrt",\n    ],' in text and '"/MT"' not in text
    assert emitter.summary["lib"] == {"label": "//src/lib:lib", "kind": "static_library"}


def test_a_precompiled_header_is_built_once_and_used_by_name():
    lib = _target("src/lib/lib.vcxproj",
                  sources=["src/lib/pch.cpp", "src/lib/a.cpp", "src/other/shared.cpp", "src/lib/plain.c"],
                  headers=["src/lib/pch.h"],
                  precompiled_header={"created_by": "src/lib/pch.cpp", "header": "pch.h",
                                      "not_using": ["src/lib/plain.c"]})
    _, files = _emit({"lib": lib})
    text = files["src/lib/BUILD.bazel"]
    assert 'load("//bazel:pch.bzl", "msvc_precompiled_header")' in text
    assert ('msvc_precompiled_header(\n    name = "lib_pch",\n    src = "pch.cpp",\n'
            '    header = "pch.h",\n    hdrs = [\n        "pch.h",\n    ],') in text
    # A source that opted out is compiled apart, without /Yu.
    nopch = text[text.index('name = "lib.nopch"'):text.index('cc_library(\n    name = "lib",')]
    assert '"plain.c"' in nopch and "/Yu" not in nopch
    # A static library's objects are taken on demand, whichever target they are in.
    assert "alwayslink" not in text
    # The rest use it; a source of another directory finds THIS project's
    # header by name, as under MSBuild.
    main = text[text.index('cc_library(\n    name = "lib",'):]
    assert '"a.cpp",\n        "//:src/other/shared.cpp",' in main
    assert '"pch.cpp"' not in main and '"plain.c"' not in main
    assert '        "/Yupch.h",\n        "/Fp$(execpath :lib_pch)",' in main
    assert '    additional_compiler_inputs = [\n        ":lib_pch",\n    ],' in main
    assert '":lib.nopch"' in main and '":lib_pch"' in main


def test_a_source_that_is_included_is_a_textual_header():
    app = _target("src/app/app.vcxproj", headers=["src/app/a.h", "src/lib/impl.cpp"])
    _, files = _emit({"app": app})
    text = files["src/app/BUILD.bazel"]
    assert ('cc_library(\n    name = "app.included",\n    textual_hdrs = [\n'
            '        "//:src/lib/impl.cpp",\n    ],\n)') in text
    main = text[text.index('name = "app",'):]
    assert '"//:src/lib/impl.cpp"' not in main and '":app.included"' in main


def test_a_dll_is_named_after_its_file_and_links_what_the_reference_linked():
    lib = _target("src/lib/lib.vcxproj")
    other = _target("src/other/other.vcxproj", kind="shared_library", output="Other.dll")
    dll = _target(
        "src/mod/mod.vcxproj", kind="shared_library", output="PowerToys.Mod.dll",
        deps=[{"name": "kernel32", "external": True, "origin": "winsdk"},
              {"name": "lib", "external": False}, {"name": "other", "external": False}],
        link_flags=["/DEF:src/mod/mod.def", "/DLL", "/GUARD:CF", "/MACHINE:X64",
                    "/MANIFESTINPUT:src/mod/app.manifest", "/ASSEMBLYRESOURCE:x.resources"],
        optimization={"compile": ["/GL"], "link": ["/OPT:REF", "/LTCG:incremental"]},
        resources=[{"script": "src/mod/mod.rc", "defines": ["UNICODE"], "options": ["/l0x0409"],
                    "includes": ["@vcpkg/x64/include"], "reads": ["src/mod/resource.h"],
                    "output": "src/mod/x64/Release/mod.res"}])
    emitter, files = _emit({"lib": lib, "other": other, "mod": dll})
    text = files["src/mod/BUILD.bazel"]
    assert 'cc_binary(\n    name = "PowerToys.Mod.dll",' in text and "    linkshared = True," in text
    assert '    win_def_file = "mod.def",' in text
    # Libraries by name, as given; /DLL and /MACHINE are the target kind and
    # the toolchain. Incremental link-time code generation keeps state beside
    # the output, which no action declares.
    assert ('    linkopts = [\n        "/OPT:REF",\n        "/LTCG",\n        "kernel32.lib",\n'
            '        "/GUARD:CF",\n        "/MANIFESTINPUT:$(execpath app.manifest)",\n    ],') in text
    assert '    additional_linker_inputs = [\n        "app.manifest",\n    ],' in text
    assert "ASSEMBLYRESOURCE" not in text
    assert any("ASSEMBLYRESOURCE" in n for n in emitter.summary["mod"]["notes"])
    # A static library by label, a DLL through its import library.
    assert '"//src/lib:lib"' in text and '"//src/other:Other.import"' in text
    # The resource script: the project directory stands in for the working
    # directory rc.exe looked files up from.
    assert ('windows_resources(\n    name = "PowerToys.Mod_res",\n    src = "mod.rc",\n'
            '    hdrs = [\n        "resource.h",\n    ],\n    defines = [\n        "UNICODE",\n    ],\n'
            '    includes = [\n        "src/mod",\n    ],') in text
    assert '":PowerToys.Mod_res"' in text
    assert 'cc_import(\n    name = "PowerToys.Mod.import",' in text
    assert emitter.summary["mod"]["label"] == "//src/mod:PowerToys.Mod.dll"


def test_libraries_archived_into_a_static_library_reach_whoever_links_it():
    lib = _target("src/lib/lib.vcxproj",
                  deps=[{"name": "version", "external": True, "origin": "winsdk"}])
    _, files = _emit({"lib": lib})
    assert '    linkopts = [\n        "version.lib",\n    ],' in files["src/lib/BUILD.bazel"]


def test_a_winrt_component_gets_its_metadata_and_projection():
    def rsp(kind, *lines):
        return [{"kind": "write", "file": "src/comp/x64/Release/comp.vcxproj.cppwinrt_%s.rsp" % kind,
                 "lines": list(lines)},
                {"kind": "exec", "cwd": "src/comp",
                 "command": '"%s" @"x64\\Release\\comp.vcxproj.cppwinrt_%s.rsp"' % (CPPWINRT, kind)}]

    comp = _target(
        "src/comp/comp.vcxproj", kind="shared_library", output="Comp.dll",
        sources=["src/comp/Thing.cpp", "src/comp/x64/Release/Generated Files/module.g.cpp"],
        winrt={"idl": ["src/comp/Thing.idl"], "midl_options": ["/W1"],
               "winmd": "src/comp/x64/Release/Merged/Contoso.Comp.winmd"},
        generated_inputs=[{"dir": "src/comp/x64/Release/Generated Files", "files": 500,
                           "examples": ["winrt/base.h"], "produced_by": "this project"}],
        steps=rsp("plat", '-in "%s\\A\\1.0.0.0\\A.winmd" -out "x64\\Release\\Generated Files\\."' % SDK)
        + rsp("comp", '-overwrite -name Contoso.Comp -pch pch.h -prefix '
                      '-comp "x64\\Release\\Generated Files\\sources" -opt '
                      '-in "C:\\ref\\src\\comp\\x64\\Release\\Merged\\Contoso.Comp.winmd" '
                      '-ref "%s\\A\\1.0.0.0\\A.winmd" -out "x64\\Release\\Generated Files\\."' % SDK))
    emitter, files = _emit({"comp": comp}, externals=_externals("comp"))
    text = files["src/comp/BUILD.bazel"]
    assert ('winrt_winmd(\n    name = "Comp_winmd",\n    srcs = [\n        "Thing.idl",\n    ],') in text
    assert '    namespace = "Contoso.Comp",' in text
    assert ('cppwinrt_component(\n    name = "Comp_component",\n'
            '    cppwinrt = "@nuget//microsoft.windows.cppwinrt/2.0.1:bin/cppwinrt.exe",\n'
            '    opts = [\n        "-overwrite",\n        "-name",\n        "Contoso.Comp",\n'
            '        "-pch",\n        "pch.h",\n        "-prefix",\n        "-opt",\n    ],\n'
            '    references = [\n        "@windows_sdk//:contract_winmds",\n    ],\n'
            '    winmds = [\n        ":Comp_winmd",\n    ],') in text
    # The one generated file that is compiled, and the include directory of the
    # generated headers, both come from the rules.
    main = text[text.index('name = "Comp.dll",'):]
    assert '":Comp_module"' in main and "module.g.cpp" not in main
    assert "Generated Files" not in main
    assert '"//bazel/cppwinrt:platform"' in main and '":Comp_component"' in main
    # The Windows SDK is projected once for every project.
    assert 'cppwinrt_projection(\n    name = "platform",' in files["bazel/cppwinrt/BUILD.bazel"]
    assert not emitter.summary["comp"].get("unhandled")


def test_packages_and_ports_a_compile_read_become_dependencies():
    lib = _target("src/lib/lib.vcxproj", external_reads={
        "@vcpkg/x64-windows-static": {"files": 16, "ports": ["spdlog"]},
        "@nuget/microsoft.windows.implementationlibrary": {"files": 5}})
    lib["compile"]["variants"][0]["includes"] += [
        "@vcpkg/x64-windows-static/include", "@nuget/microsoft.windows.implementationlibrary/include"]
    emitter, files = _emit({"lib": lib}, externals=_externals("lib"))
    text = files["src/lib/BUILD.bazel"]
    # The port whose headers were read, and the install tree's include
    # directory as such: it is on the command line whatever is read from it.
    assert ('    deps = [\n        "@nuget//microsoft.windows.implementationlibrary/1.0.2:cc.headers",\n'
            '        "@vcpkg//:include",\n        "@vcpkg//:spdlog",\n    ],') in text
    assert "@vcpkg/x64" not in text.split("deps = [")[0]        # no include path for them
    # A package directory on the command line that nothing was read from still
    # gets its search path, when the lock knows the package's include directories.
    unread = _target("src/lib/lib.vcxproj")
    unread["compile"]["variants"][0]["includes"] += [
        "@vcpkg/x64-windows-static/include", "@nuget/microsoft.windows.implementationlibrary/include"]
    lock = {"targets": {}, "packages": {"microsoft.windows.implementationlibrary/1.0.2": {
        "cc": {"include_dirs": ["include"]}}}}
    _, files = _emit({"lib": unread}, lock=lock, externals=_externals("lib"))
    assert ('    deps = [\n        "@nuget//microsoft.windows.implementationlibrary/1.0.2:cc.headers",\n'
            '        "@vcpkg//:include",\n    ],') in files["src/lib/BUILD.bazel"]
    # A package nobody recorded for the target cannot be guessed at.
    emitter, files = _emit({"lib": lib})
    assert "src/lib/BUILD.bazel" not in files
    assert "not recorded for this target" in emitter.summary["lib"]["unhandled"][0]


def test_a_file_a_build_step_generated_is_copied_or_comes_from_a_named_rule():
    generated = [{"dir": "src/common", "files": 1, "names": ["version/Generated Files/version_gen.h"],
                  "examples": ["version/Generated Files/version_gen.h"], "produced_by": "another project"}]
    lib = _target("src/lib/lib.vcxproj", generated_inputs=generated)
    existing = {"src/lib/a.cpp"}
    emitter, files = _emit({"lib": lib}, existing=existing)
    # Not in the workspace: taken from the reference tree, and said so.
    assert emitter.snapshots == {
        "src/common/version/Generated Files/version_gen.h": "read by the compiles of lib"}
    assert '"//:src/common/version/Generated Files/version_gen.h"' in files["src/lib/BUILD.bazel"]
    # With a rule named for it, the rule provides it and nothing is copied.
    hooks = [{"match": "version/Generated Files/", "deps": ["//src/common/version:generated"]}]
    emitter, files = _emit({"lib": lib}, existing=existing, hooks=hooks)
    assert emitter.snapshots == {}
    assert '"//src/common/version:generated"' in files["src/lib/BUILD.bazel"]
    assert "version_gen.h" not in files["src/lib/BUILD.bazel"]


def test_a_source_a_package_has_its_consumers_compile_is_a_label_of_the_package():
    root = "C:/Users/me/.nuget/packages/microsoft.windows.implementationlibrary/1.0.2"
    lib = _target("src/lib/lib.vcxproj", sources=["src/lib/a.cpp", root + "/include/init.cpp"])
    emitter, files = _emit({"lib": lib}, externals=_externals("lib"))
    assert ('"@nuget//microsoft.windows.implementationlibrary/1.0.2:include/init.cpp"'
            in files["src/lib/BUILD.bazel"])
    # The differ pairs it by the package's token. The reference side has it from
    # the model, so the configuration names the place Bazel has the package in
    # and no directory of the machine the reference was built on.
    ignore = emit_cc.config_suggestions(emitter)["ignore"]
    assert ignore["source_dir_map"] == [{
        "from": "external/+nuget_assets+nuget/microsoft.windows.implementationlibrary/1.0.2",
        "to": "@nuget/microsoft.windows.implementationlibrary"}]
    assert not [rule for rules in ignore.values() for rule in rules
                if isinstance(rule, dict) and ":/" in rule["from"]]


def test_what_cannot_be_expressed_is_not_written():
    odd = _target("src/odd/odd.vcxproj", generated_inputs=[
        {"dir": "src/odd/x64/Release/gen", "files": 300, "examples": ["a.h"], "produced_by": "this project"}])
    emitter, files = _emit({"odd": odd})
    assert files == {}
    assert emitter.summary["odd"]["unhandled"] == [
        "300 generated files under src/odd/x64/Release/gen and no rule known for them"]


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
