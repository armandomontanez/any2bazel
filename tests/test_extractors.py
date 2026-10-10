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

"""End-to-end test: synthetic File-API + aquery JSON -> models -> diff.

The JSON fixtures here mirror the real schemas (codemodel-v2 target objects;
aquery ActionGraphContainer). They let us validate the extractors and the full
pipeline without cmake/bazel installed. Replace with captured real output once
available -- the extractor code does not change.

The fixture describes the SAME tiny project from both sides:
  target 'mylib' (static): compiles src/a.cpp with -DFOO=1 -Iinclude -std=c++17
and asserts the pipeline converges (zero errors), even though the bazel argv
carries the usual injected toolchain defaults.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import extract_bazel
import extract_cmake
from diff import diff_models, summarize
from reconstruct import reconstruct

REPO = "/work/proj"


def _view(model, name):
    """Reconstruct a target into its comparison view (TUs/link_flags/deps).
    Extractors now emit raw actions; the differ reconstructs -- tests assert on
    the reconstructed view, the same shape the diff compares."""
    return reconstruct(model)[name]

# ---- CMake File API codemodel reply (minimal but real-shaped) ----------------
CODEMODEL = {
    "kind": "codemodel",
    "configurations": [{
        "name": "Debug",
        "targets": [{"id": "mylib::@abc", "name": "mylib", "jsonFile": "target-mylib.json"}],
    }],
}
TARGET_MYLIB = {
    "name": "mylib",
    "type": "STATIC_LIBRARY",
    "sources": [{"path": "src/a.cpp"}],
    "compileGroups": [{
        "language": "CXX",
        "sourceIndexes": [0],
        "compileCommandFragments": [{"fragment": "-std=c++17 -Wall"}],
        "defines": [{"define": "FOO=1"}],
        "includes": [{"path": "/work/proj/include"}],
    }],
    "dependencies": [],
}

# ---- Bazel aquery ActionGraphContainer (same project) ------------------------
AQUERY = {
    "artifacts": [
        {"id": 1, "pathFragmentId": 10},   # src/a.cpp
        {"id": 2, "pathFragmentId": 11},   # libmylib.a
    ],
    "pathFragments": [
        {"id": 10, "label": "a.cpp", "parentId": 12},
        {"id": 12, "label": "src"},
        {"id": 11, "label": "libmylib.a", "parentId": 13},
        {"id": 13, "label": "bazel-out"},
    ],
    "targets": [{"id": 100, "label": "//:mylib"}],
    "actions": [
        {
            "mnemonic": "CppCompile",
            "targetId": 100,
            "outputIds": [],
            "arguments": [
                "/usr/bin/clang", "-c", "src/a.cpp",
                "-DFOO=1", "-I", "/work/proj/include", "-std=c++17", "-Wall",
                # injected toolchain defaults that must NOT cause a diff:
                "-iquote", "/work/proj", "-fno-canonical-system-headers",
                "-frandom-seed=xyz", "-g0", "-O2",
            ],
        },
        {
            "mnemonic": "CppArchive",
            "targetId": 100,
            "outputIds": [2],
            "arguments": ["/usr/bin/ar", "rcs", "libmylib.a", "a.o"],
        },
    ],
}


def _write_cmake_fixture(root):
    reply = os.path.join(root, "build", ".cmake", "api", "v1", "reply")
    os.makedirs(reply)
    with open(os.path.join(reply, "index-0.json"), "w") as f:
        json.dump({"objects": [{"kind": "codemodel", "jsonFile": "codemodel.json"}]}, f)
    with open(os.path.join(reply, "codemodel.json"), "w") as f:
        json.dump(CODEMODEL, f)
    with open(os.path.join(reply, "target-mylib.json"), "w") as f:
        json.dump(TARGET_MYLIB, f)
    return os.path.join(root, "build")


def test_full_pipeline_converges():
    with tempfile.TemporaryDirectory() as root:
        build = _write_cmake_fixture(root)
        a = extract_cmake.extract(build, REPO)

        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(AQUERY, f)
        b = extract_bazel.extract(aq_path, REPO)

        # CMake keeps the target name; Bazel keys by full label (//:mylib ->
        # ':mylib') to avoid cross-package name collisions. Library comparison
        # is by source path, so the differing names still converge.
        assert "mylib" in a.targets, a.targets.keys()
        assert ":mylib" in b.targets, b.targets.keys()
        assert a.targets["mylib"].kind.value == "static_library"
        assert b.targets[":mylib"].kind.value == "static_library"

        res = summarize(diff_models(a, b))
        assert res["converged"], json.dumps(res, indent=2)
        assert res["errors"] == 0, json.dumps(res, indent=2)


def test_cmake_extracts_canonical_flags():
    with tempfile.TemporaryDirectory() as root:
        build = _write_cmake_fixture(root)
        a = extract_cmake.extract(build, REPO)
        tu = _view(a, "mylib").tus[0]
        assert tu.source == "src/a.cpp"
        assert "FOO=1" in tu.defines
        assert "include" in tu.includes          # made repo-relative
        assert "-std=c++17" in tu.flags


def test_bazel_infers_kind_and_drops_defaults():
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(AQUERY, f)
        b = extract_bazel.extract(aq_path, REPO)
        tu = _view(b, ":mylib").tus[0]   # full-label key
        # toolchain defaults must be gone after canonicalization
        assert not any("random-seed" in fl for fl in tu.flags)
        assert "-fno-canonical-system-headers" not in tu.flags
        assert "include" in tu.includes


def test_header_processing_actions_are_not_tus():
    # Bazel's parse_headers/layering_check feature compiles headers standalone
    # (-xc++-header) to check self-containment. These are NOT translation units
    # and must not appear in the model (else every header is a spurious extra_tu).
    aquery = {
        "artifacts": [{"id": 1, "pathFragmentId": 10}],
        "pathFragments": [{"id": 10, "label": "re2.h", "parentId": 11},
                          {"id": 11, "label": "re2"}],
        "targets": [{"id": 100, "label": "//:re2"}],
        "actions": [
            {"mnemonic": "CppCompile", "targetId": 100, "outputIds": [],
             "arguments": ["clang", "-xc++-header", "-c", "re2/re2.h",
                           "-o", "re2/re2.h.processed", "-DFOO=1"]},
            {"mnemonic": "CppCompile", "targetId": 100, "outputIds": [],
             "arguments": ["clang", "-c", "re2/re2.cc", "-o", "re2.o", "-DFOO=1"]},
        ],
    }
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(aquery, f)
        b = extract_bazel.extract(aq_path, REPO)
        views = reconstruct(b)
        srcs = [tu.source for v in views.values() for tu in v.tus]
        assert "re2/re2.cc" in srcs, srcs           # real TU kept
        assert not any(s.endswith(".h") for s in srcs), srcs  # header dropped


def test_bazel_objccompile_is_a_real_compile():
    # objc_library sources (.m/.mm) show up under Bazel's ObjcCompile mnemonic,
    # not CppCompile. CMake's File API has no such split -- it reports them as
    # CppCompile too -- so ObjcCompile must land in the same TU bucket, or an
    # Objective-C target's sources silently vanish from the Bazel model (they
    # still compile fine; only the extractor's view of them was missing).
    aquery = {
        "artifacts": [{"id": 1, "pathFragmentId": 10}],
        "pathFragments": [{"id": 10, "label": "cocoa_init.o", "parentId": 11},
                          {"id": 11, "label": "bazel-out"}],
        "targets": [{"id": 100, "label": "//:glfw_cocoa"}],
        "actions": [
            {"mnemonic": "ObjcCompile", "targetId": 100, "outputIds": [1],
             "arguments": ["clang", "-c", "src/cocoa_init.m",
                           "-o", "bazel-out/cocoa_init.o", "-DFOO=1"]},
        ],
    }
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(aquery, f)
        b = extract_bazel.extract(aq_path, REPO)
        t = _view(b, ":glfw_cocoa")
        srcs = [tu.source for tu in t.tus]
        assert "src/cocoa_init.m" in srcs, srcs
        assert t.role.value == "production", t.role


def test_bazel_extracts_link_flags_from_cpplink():
    # A CppLink action: link flags must be extracted; driver mechanics (wrapper,
    # -o/output), object/archive inputs and -l libs must be dropped.
    aquery = {
        "artifacts": [{"id": 1, "pathFragmentId": 10}],
        "pathFragments": [{"id": 10, "label": "app", "parentId": 11},
                          {"id": 11, "label": "bazel-out"}],
        "targets": [{"id": 100, "label": "//:app"}],
        "actions": [
            {"mnemonic": "CppCompile", "targetId": 100, "outputIds": [],
             "arguments": ["clang", "-c", "main.cc", "-o", "main.o", "-DFOO=1"]},
            {"mnemonic": "CppLink", "targetId": 100, "outputIds": [1],
             "arguments": ["cc_wrapper.sh", "-o", "bazel-out/app",
                           "bazel-out/_objs/app/main.o", "bazel-out/libfoo.a",
                           "-pthread", "-Wl,--gc-sections", "-lm",
                           "-headerpad_max_install_names"]},
        ],
    }
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(aquery, f)
        b = extract_bazel.extract(aq_path, REPO)
        t = _view(b, ":app")
        assert t.kind.value == "executable"
        assert "-pthread" in t.link_flags
        assert "-Wl,--gc-sections" in t.link_flags
        # inputs / -l / driver / toolchain noise must NOT be link flags
        for junk in ("-o", "-lm", "-headerpad_max_install_names"):
            assert junk not in t.link_flags, (junk, t.link_flags)
        assert not any(f.endswith((".o", ".a")) for f in t.link_flags)
        # the in-project archive becomes an INTERNAL dep (not external, not a flag)
        foo = next((d for d in t.deps if d.name == "foo"), None)
        assert foo is not None and not foo.external, t.deps


def test_bazel_external_archive_is_external_dep():
    # An archive linked by PATH from external/ (a bzlmod module, e.g. catch2)
    # must be captured as an EXTERNAL dep -- previously dropped (only -l deps
    # were captured), which is the spdlog Catch2Main gap.
    aquery = {
        "artifacts": [{"id": 1, "pathFragmentId": 10}],
        "pathFragments": [{"id": 10, "label": "app", "parentId": 11},
                          {"id": 11, "label": "bazel-out"}],
        "targets": [{"id": 100, "label": "//:app"}],
        "actions": [
            {"mnemonic": "CppCompile", "targetId": 100, "outputIds": [],
             "arguments": ["clang", "-c", "main.cc", "-o", "main.o"]},
            {"mnemonic": "CppLink", "targetId": 100, "outputIds": [1],
             "arguments": ["cc_wrapper.sh", "-o", "bazel-out/app",
                           "bazel-out/_objs/app/main.o",
                           "bazel-out/bin/libmylib.a",                     # internal
                           "bazel-out/bin/external/catch2+/libcatch2_main.a"]},  # external
        ],
    }
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(aquery, f)
        b = extract_bazel.extract(aq_path, REPO)
        deps = {d.name: d.external for d in _view(b, ":app").deps}
        assert deps.get("catch2_main") is True, deps   # external archive captured
        assert deps.get("mylib") is False, deps         # internal archive captured


def test_bazel_recovers_javac_as_javacompile():
    # Bazel's Java compile mnemonic is 'Javac'; the extractor maps it to the
    # neutral 'JavaCompile' so it reconstructs into a CompileGroup, same as
    # Maven. Turbine (header/ijar) and JavaSourceJar (packaging) are skipped.
    aquery = {
        "artifacts": [{"id": 1, "pathFragmentId": 10}],
        "pathFragments": [{"id": 10, "label": "libguava.jar", "parentId": 11},
                          {"id": 11, "label": "bazel-out"}],
        "targets": [{"id": 100, "label": "//guava:guava"}],
        "actions": [
            {"mnemonic": "Javac", "targetId": 100, "outputIds": [1],
             "arguments": ["java", "-jar", "JavaBuilder.jar",
                           "guava/src/com/x/A.java", "guava/src/com/x/B.java"]},
            {"mnemonic": "Turbine", "targetId": 100, "outputIds": [],
             "arguments": ["turbine", "guava/src/com/x/A.java"]},
            {"mnemonic": "JavaSourceJar", "targetId": 100, "outputIds": [],
             "arguments": ["zip", "src.jar"]},
        ],
    }
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(aquery, f)
        b = extract_bazel.extract(aq_path, "/repo")
        t = b.targets["guava:guava"]
        # exactly one action, the Javac mapped to JavaCompile (Turbine/SourceJar
        # dropped)
        mnems = [a.mnemonic for a in t.actions]
        assert mnems == ["JavaCompile"], mnems
        v = _view(b, "guava:guava")
        assert len(v.compile_groups) == 1
        assert v.compile_groups[0].sources == ("guava/src/com/x/A.java",
                                               "guava/src/com/x/B.java")


# ---- depSet resolution (aquery input closures) ------------------------------
# Regression tests for the DAG walk behind TsProgram's input closure. This path
# had NO coverage, and the original implementation was unusable on any large
# graph: on cloudflare/workerd it was OOM-killed at 2.7GB having enumerated 349M
# artifact IDs, because it counted PATHS through a shared DAG rather than nodes.

def _depset_container(dsets):
    return {"depSetOfFiles": [dict(d) for d in dsets]}


def test_depsets_resolve_transitively():
    r = extract_bazel.DepsetResolver(_depset_container([
        {"id": 1, "directArtifactIds": [10], "transitiveDepSetIds": [2, 3]},
        {"id": 2, "directArtifactIds": [20]},
        {"id": 3, "directArtifactIds": [30], "transitiveDepSetIds": [4]},
        {"id": 4, "directArtifactIds": [40]},
    ]))
    assert sorted(r.resolve([1])) == [10, 20, 30, 40]
    # a leaf depSet resolves to just its own artifacts
    assert sorted(r.resolve([3])) == [30, 40]
    # unions of several depSets, and unknown ids, are handled
    assert sorted(r.resolve([2, 4])) == [20, 40]
    assert r.resolve([999]) == []
    assert r.resolve([]) == []


def test_depsets_dedupe_diamond():
    """A depSet reachable by two paths must be counted ONCE.

    The original implementation concatenated child LISTS, so a shared node was
    repeated once per path through the DAG -- counting paths, not nodes.
    """
    r = extract_bazel.DepsetResolver(_depset_container([
        {"id": 1, "transitiveDepSetIds": [2, 3]},
        {"id": 2, "directArtifactIds": [99], "transitiveDepSetIds": [4]},
        {"id": 3, "directArtifactIds": [99], "transitiveDepSetIds": [4]},
        {"id": 4, "directArtifactIds": [99, 100]},
    ]))
    assert sorted(r.resolve([1])) == [99, 100]


def test_depsets_do_not_blow_up_on_shared_dag():
    """The workerd blocker, in miniature.

    A chain where every level references the two levels below it has a number of
    ROOT->LEAF PATHS that grows like Fibonacci, while the node count grows
    linearly. Path-counting flattening is therefore exponential in the depth: at
    depth 60 it would enumerate ~1e12 entries. A visited-set walk is linear, so
    this returns immediately.
    """
    depth = 60
    dsets = [{"id": 1, "directArtifactIds": [1]},
             {"id": 2, "directArtifactIds": [2]}]
    for i in range(3, depth + 1):
        dsets.append({"id": i, "directArtifactIds": [i],
                      "transitiveDepSetIds": [i - 1, i - 2]})
    r = extract_bazel.DepsetResolver(_depset_container(dsets))
    assert sorted(r.resolve([depth])) == list(range(1, depth + 1))


def test_depsets_survive_a_cycle():
    """Defensive: a self- or mutually-referencing depSet must not hang.

    Real aquery output is acyclic, but the original recursion would have
    recursed forever (RecursionError) on malformed input rather than degrading.
    """
    r = extract_bazel.DepsetResolver(_depset_container([
        {"id": 1, "directArtifactIds": [10], "transitiveDepSetIds": [2]},
        {"id": 2, "directArtifactIds": [20], "transitiveDepSetIds": [1, 2]},
    ]))
    assert sorted(r.resolve([1])) == [10, 20]


class _WatchedDepset(dict):
    """A depSet that records whether anything read its contents."""

    def get(self, key, default=None):
        if key in ("directArtifactIds", "transitiveDepSetIds"):
            self["_read"] = True
        return dict.get(self, key, default)


def test_depsets_are_not_indexed_eagerly():
    """Resolution is on demand: only the depSets actually reached are walked.

    Only the TS path consumes input closures, so a build with no such action
    (i.e. every C++ project, and workerd) must pay nothing for the rest of the
    graph. The original implementation indexed ALL depSets up front and threw
    the result away.

    Checked behaviourally: an unreachable component must never be read. Doing
    this with a watcher rather than by timing/memory keeps the failure a clean
    assertion instead of an OOM.
    """
    reached = _WatchedDepset({"id": 1, "directArtifactIds": [10]})
    unreached = _WatchedDepset({"id": 2, "directArtifactIds": [20],
                                "transitiveDepSetIds": [3]})
    r = extract_bazel.DepsetResolver({"depSetOfFiles": [reached, unreached]})
    assert not reached.get("_read") and not unreached.get("_read"), \
        "resolver walked the DAG at construction time"
    assert r.resolve([1]) == [10]
    assert reached.get("_read"), "reached depSet was not read"
    assert not unreached.get("_read"), \
        "resolver walked a depSet outside the requested closure"


def test_ts_program_splits_into_per_file_tscompile_actions():
    """TsProgram -> one TsCompile per real .ts source, via the input closure.

    Also covers the filtering: .d.ts (type-only), non-src/ inputs (node_modules,
    tsconfig) produce no TsCompile action.
    """
    aquery = {
        "artifacts": [
            {"id": 1, "pathFragmentId": 1},   # src/a.ts        -> emitted
            {"id": 2, "pathFragmentId": 2},   # src/b.d.ts      -> type-only
            {"id": 3, "pathFragmentId": 3},   # node_modules/x.ts -> not src/
            {"id": 4, "pathFragmentId": 4},   # src/sub/c.ts    -> emitted
        ],
        "pathFragments": [
            {"id": 10, "label": "src"},
            {"id": 1, "label": "a.ts", "parentId": 10},
            {"id": 2, "label": "b.d.ts", "parentId": 10},
            {"id": 11, "label": "node_modules"},
            {"id": 3, "label": "x.ts", "parentId": 11},
            {"id": 12, "label": "sub", "parentId": 10},
            {"id": 4, "label": "c.ts", "parentId": 12},
        ],
        "targets": [{"id": 1, "label": "//:ts"}],
        # the sources arrive through a TRANSITIVE depSet, not a flat one
        "depSetOfFiles": [
            {"id": 1, "directArtifactIds": [1, 2], "transitiveDepSetIds": [2]},
            {"id": 2, "directArtifactIds": [3, 4]},
        ],
        "actions": [{
            "mnemonic": "TsProgram", "targetId": 1, "inputDepSetIds": [1],
            "arguments": ["tsc", "--outDir",
                          "bazel-out/k8-fastbuild/bin/out-build"],
        }],
    }
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "w") as f:
            json.dump(aquery, f)
        b = extract_bazel.extract(aq_path, "/repo")
        t = b.targets["<tscompile>"]
        assert all(a.mnemonic == "TsCompile" for a in t.actions)
        assert sorted(a.inputs[0] for a in t.actions) == ["src/a.ts",
                                                          "src/sub/c.ts"]
        outs = {a.inputs[0]: a.outputs for a in t.actions}
        assert outs["src/a.ts"] == ("out-build/a.js", "out-build/a.js.map")
        assert outs["src/sub/c.ts"] == ("out-build/sub/c.js",
                                        "out-build/sub/c.js.map")
        # role is set by the extractor and must not be demoted to AGGREGATE
        assert t.role.value == "production", t.role


# ---- Bazel on Windows (MSVC toolchain) ---------------------------------------

_MSVC_CL = ("C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Tools/MSVC/"
            "14.44.35207/bin/HostX64/x64/cl.exe")
_MSVC_INCLUDE = ("C:\\Program Files (x86)\\Microsoft Visual Studio\\2022\\BuildTools\\VC\\Tools"
                 "\\MSVC\\14.44.35207\\include;C:\\Program Files (x86)\\Windows Kits\\10\\include"
                 "\\10.0.26100.0\\ucrt;C:\\Program Files (x86)\\Windows Kits\\10\\include"
                 "\\10.0.26100.0\\um")


def _windows_aquery():
    """A cc_library and a cc_binary as rules_cc's MSVC toolchain reports them.
    Under `//...` a Windows cc_library emits an archive AND a DLL."""
    return {
        "artifacts": [{"id": 1, "pathFragmentId": 1}, {"id": 2, "pathFragmentId": 2},
                      {"id": 3, "pathFragmentId": 3}],
        "pathFragments": [{"id": 1, "label": "core.lib"}, {"id": 2, "label": "core.dll"},
                          {"id": 3, "label": "tool.exe"}],
        "targets": [{"id": 1, "label": "//src:core"}, {"id": 2, "label": "//src:tool"}],
        "actions": [
            {"mnemonic": "CppCompile", "targetId": 1, "outputIds": [],
             "arguments": [_MSVC_CL, "/nologo", "/DNOMINMAX", "/I.", "/MD", "/c",
                           "src/core.cpp", "/Fobazel-out/x64_windows-opt/bin/core.obj"],
             "environmentVariables": [{"key": "INCLUDE", "value": _MSVC_INCLUDE},
                                      {"key": "PATH", "value": "C:\\x"}]},
            # Deliberately archive first, then the DLL link.
            {"mnemonic": "CppArchive", "targetId": 1, "outputIds": [1],
             "arguments": ["lib.exe", "/OUT:core.lib", "core.obj"]},
            {"mnemonic": "CppLink", "targetId": 1, "outputIds": [2],
             "arguments": ["link.exe", "/DLL", "/OUT:core.dll", "core.obj"]},
            {"mnemonic": "CppCompile", "targetId": 2, "outputIds": [],
             "arguments": [_MSVC_CL, "/nologo", "/c", "src/main.cpp"]},
            {"mnemonic": "CppLink", "targetId": 2, "outputIds": [3],
             "arguments": ["link.exe", "/OUT:tool.exe", "main.obj", "core.lib"]},
        ],
    }


def _extract_bazel(aquery, raw=None):
    with tempfile.TemporaryDirectory() as root:
        aq_path = os.path.join(root, "aquery.json")
        with open(aq_path, "wb") as f:
            f.write(raw if raw is not None else json.dumps(aquery).encode("utf-8"))
        return extract_bazel.extract(aq_path, "C:\\work\\proj")


def test_bazel_msvc_toolchain_facts_are_recorded():
    b = _extract_bazel(_windows_aquery())
    # Version and target arch from the compiler path; the SDK version from the
    # INCLUDE environment, where the toolchain passes system include dirs.
    assert b.toolchain == {"compiler": "msvc", "msvc_version": "14.44.35207",
                           "target_arch": "x64", "winsdk_version": "10.0.26100.0"}


def test_non_msvc_bazel_model_records_no_toolchain():
    assert _extract_bazel(AQUERY).toolchain == {}


def test_windows_cc_library_with_archive_and_dll_stays_a_library():
    b = _extract_bazel(_windows_aquery())
    assert b.targets["src:core"].kind.value == "static_library"
    assert b.targets["src:tool"].kind.value == "executable"
    tu = _view(b, "src:core").tus[0]
    assert tu.source == "src/core.cpp"
    assert tu.defines == ("NOMINMAX",) and "/MD" in tu.flags


def test_bazel_reads_an_aquery_file_powershell_wrote_as_utf16():
    # `bazel aquery ... > aquery.json` in PowerShell 5 produces UTF-16 with a BOM.
    b = _extract_bazel(None, raw=json.dumps(_windows_aquery()).encode("utf-16"))
    assert "src:core" in b.targets


def test_bazel_param_file_contents_are_spliced_into_argv():
    # rules_cc's MSVC toolchain will not let the linker param file be switched
    # off; `aquery --include_param_files` reports its content instead.
    aquery = _windows_aquery()
    link = aquery["actions"][-1]
    link["arguments"] = ["link.exe", "@bazel-out/x64_windows-opt/bin/tool.exe-2.params"]
    link["paramFiles"] = [{"execPath": "bazel-out/x64_windows-opt/bin/tool.exe-2.params",
                           "arguments": ["/OUT:tool.exe", "/SUBSYSTEM:WINDOWS", "main.obj",
                                         "advapi32.lib"]}]
    b = _extract_bazel(aquery)
    view = _view(b, "src:tool")
    assert view.link_flags == ("/SUBSYSTEM:WINDOWS",)
    assert [(d.name, d.external) for d in view.deps] == [("advapi32", True)]
    # An unknown @file is left alone rather than dropped.
    link["arguments"].append("@elsewhere.params")
    assert "@elsewhere.params" in _extract_bazel(aquery).targets["src:tool"].actions[-1].arguments


def test_bazel_csharp_sources_come_from_declared_inputs():
    # rules_dotnet passes sources in a param file that aquery never writes.
    aquery = {
        "artifacts": [{"id": 1, "pathFragmentId": 1}, {"id": 2, "pathFragmentId": 2},
                      {"id": 3, "pathFragmentId": 3}],
        "pathFragments": [{"id": 10, "label": "src"}, {"id": 1, "label": "Program.cs", "parentId": 10},
                          {"id": 2, "label": "System.Runtime.dll"}, {"id": 3, "label": "tool.dll"}],
        "targets": [{"id": 1, "label": "//src:tool"}],
        "depSetOfFiles": [{"id": 1, "directArtifactIds": [1, 2]}],
        "actions": [{"mnemonic": "CSharpCompile", "targetId": 1, "inputDepSetIds": [1],
                     "outputIds": [3], "arguments": ["dotnet", "csc.dll", "@tool.rsp"]}],
    }
    b = _extract_bazel(aquery)
    (action,) = b.targets["src:tool"].actions
    assert action.mnemonic == "CSharpCompile"
    assert [p.replace("\\", "/") for p in action.inputs] == ["src/Program.cs"]
    assert b.targets["src:tool"].role.value == "production"
    (group,) = _view(b, "src:tool").compile_groups
    assert (group.language, group.sources) == ("csharp", ("src/Program.cs",))


def test_bazel_csharp_tools_of_the_build_itself_are_not_deliverables():
    # A converter the build runs is compiled for the exec configuration; a
    # library's reference-only twin is the same compile twice. Neither is an
    # assembly of the project.
    leaf_ids = []       # path fragment id of each path's last component

    def fragments(*paths):
        out, ids = [], {}
        for path in paths:
            parent = None
            for part in path.split("/"):
                key = (parent, part)
                if key not in ids:
                    ids[key] = len(ids) + 1
                    out.append(dict({"id": ids[key], "label": part},
                                    **({"parentId": parent} if parent else {})))
                parent = ids[key]
            leaf_ids.append(parent)
        return out

    paths = ["bazel-out/x64_windows-opt-exec-ST-0123456789ab/bin/tools/conv/net8.0/conv.dll",
             "bazel-out/x64_windows-opt-ST-0123456789ab/bin/src/lib/lib/net8.0/Lib.dll",
             "bazel-out/x64_windows-opt-ST-0123456789ab/bin/src/lib/lib/net8.0/ref/Lib.dll"]
    path_fragments = fragments(*paths)
    aquery = {
        "artifacts": [{"id": i + 1, "pathFragmentId": f} for i, f in enumerate(leaf_ids)],
        "pathFragments": path_fragments,
        "targets": [{"id": 1, "label": "//tools/conv:conv"}, {"id": 2, "label": "//src/lib:lib"}],
        "depSetOfFiles": [],
        "actions": [
            {"mnemonic": "CSharpCompile", "targetId": 1, "outputIds": [1],
             "arguments": ["wrapper.bat", "dotnet.exe", "csc.dll", "/target:exe", "/out:" + paths[0], "C.cs"]},
            {"mnemonic": "CSharpCompile", "targetId": 2, "outputIds": [2],
             "arguments": ["wrapper.bat", "dotnet.exe", "csc.dll", "/target:library", "/out:" + paths[1], "L.cs"]},
            {"mnemonic": "CSharpCompile", "targetId": 2, "outputIds": [3],
             "arguments": ["wrapper.bat", "dotnet.exe", "csc.dll", "/refonly", "/out:" + paths[2], "L.cs"]}],
    }
    b = _extract_bazel(aquery)
    assert sorted(b.targets) == ["src/lib:lib"]
    assert len(b.targets["src/lib:lib"].actions) == 1


def test_model_annotations_round_trip_and_stay_out_of_old_models():
    from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                       TargetKind, TargetRole)
    from serialize import dump_model, load_model
    m = CanonicalModel(build_system=BuildSystem.MSBUILD, repo_root="C:\\work\\proj",
                       config={"configuration": "Release"},
                       toolchain={"msvc_version": "14.44.35207"},
                       path_map=[("C:\\work\\proj\\packages\\Foo.1.2.3", "@nuget/foo")])
    m.add(Target("app", TargetKind.EXECUTABLE, role=TargetRole.PACKAGE,
                 actions=[Action("CppCompile", ("cl.exe", "a.cpp"), cwd="C:\\work\\proj\\src")],
                 deps=[Dependency("kernel32", True, origin="winsdk", version="10.0.26100.0")],
                 attrs={"project": "src/app.vcxproj"}))
    plain = CanonicalModel(build_system=BuildSystem.CMAKE, repo_root="/work")
    plain.add(Target("lib", TargetKind.STATIC, role=TargetRole.PRODUCTION,
                     actions=[Action("CppCompile", ("-c", "a.cc"))],
                     deps=[Dependency("z", True)]))
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "m.json")
        dump_model(m, path)
        back = load_model(path)
        assert back.config == m.config and back.toolchain == m.toolchain
        assert back.path_map == m.path_map
        t = back.targets["app"]
        assert t.role == TargetRole.PACKAGE and t.attrs == {"project": "src/app.vcxproj"}
        assert t.actions[0].cwd == "C:\\work\\proj\\src"
        assert t.deps == [Dependency("kernel32", True, "winsdk", "10.0.26100.0")]
        # A model that sets none of them serializes exactly as it used to.
        dump_model(plain, path)
        with open(path) as f:
            obj = json.load(f)
        assert set(obj) == {"build_system", "repo_root", "targets", "configured_files"}
        assert set(obj["targets"]["lib"]) == {"kind", "role", "deps", "actions"}
        assert obj["targets"]["lib"]["deps"] == [{"name": "z", "external": True}]
        assert set(obj["targets"]["lib"]["actions"][0]) == {"mnemonic", "arguments", "inputs",
                                                            "outputs"}


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
