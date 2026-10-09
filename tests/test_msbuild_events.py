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

"""Correlating the MSBuild event dump. The records are the shapes the logger
writes when MSBuild replays a real binlog; replaying itself needs MSBuild and
is not exercised here."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import msbuild_events as events

PROJ = "C:\\work\\proj\\src\\lib\\lib.vcxproj"

RECORDS = [
    {"e": "Dump", "schema": 1},
    {"e": "BuildStarted"},
    {"e": "LongValue", "id": 0, "value": "<SolutionConfiguration>...</SolutionConfiguration>"},
    {"e": "Eval", "ev": 7, "file": PROJ, "global": {"Configuration": "Release", "Big": "@long:0"},
     "props": {"ConfigurationType": "StaticLibrary", "IntDir": "x64\\Release\\"},
     "items": [{"t": "ClCompile", "i": "a.cpp", "m": {"PrecompiledHeader": "Use"}},
               {"t": "ClCompile", "i": "pch.cpp", "m": {"PrecompiledHeader": "Create"}}]},
    {"e": "Import", "ev": 7, "file": PROJ, "imported": "C:\\work\\proj\\Directory.Build.props"},
    {"e": "EnvRead", "name": "VCPKG_ROOT"},
    {"e": "EnvRead", "name": "VCPKG_ROOT"},
    {"e": "ProjectStarted", "p": 3, "ev": 7, "file": PROJ, "targets": "", "parent": -2,
     "parentTask": -1, "global": {"Configuration": "Release", "Big": "@long:0"}},
    {"e": "TargetStarted", "p": 3, "t": 10, "name": "ClCompile", "file": "Microsoft.CppCommon.targets"},
    {"e": "TaskStarted", "p": 3, "t": 10, "k": 100, "name": "CL", "file": "x.targets", "line": 5},
    {"e": "TaskParameter", "p": 3, "t": 10, "k": 100, "kind": "TaskInput", "name": "Sources",
     "itemType": "Sources", "items": [{"i": "a.cpp", "m": {}}, {"i": "pch.cpp", "m": {}}]},
    {"e": "TaskParameter", "p": 3, "t": 10, "k": 100, "kind": "TaskInput",
     "name": "TrackerLogDirectory", "itemType": "TrackerLogDirectory",
     "items": [{"i": "x64\\Release\\lib.tlog\\"}]},
    {"e": "CommandLine", "p": 3, "t": 10, "k": 100, "task": "CL", "cmd": "CL.exe /c a.cpp"},
    {"e": "TaskParameter", "p": 3, "t": 10, "k": 100, "kind": "TaskOutput", "name": "Objs",
     "itemType": "Objs", "items": [{"i": "x64\\Release\\a.obj", "m": {}}]},
    {"e": "TaskFinished", "p": 3, "t": 10, "k": 100, "name": "CL", "succeeded": True},
    {"e": "TaskParameter", "p": 3, "t": 10, "kind": "AddItem", "name": "ClCompile",
     "itemType": "ClCompile", "items": [{"i": "gen\\module.g.cpp", "m": {}}]},
    {"e": "TargetFinished", "p": 3, "t": 10, "name": "ClCompile", "succeeded": True},
    {"e": "TargetSkipped", "p": 3, "t": 11, "name": "GenerateVersionData",
     "reason": "OutputsUpToDate"},
    {"e": "TaskParameter", "p": 3, "t": 11, "kind": "SkippedTargetInputs",
     "items": [{"i": "C:\\work\\proj\\Version.props"}]},
    {"e": "TaskParameter", "p": 3, "t": 11, "kind": "SkippedTargetOutputs",
     "items": [{"i": "C:\\work\\proj\\src\\lib\\Generated Files\\version_gen.h"}]},
    {"e": "TargetSkipped", "p": 3, "name": "Midl", "reason": "ConditionWasFalse",
     "condition": "'@(Midl)' != ''"},
    {"e": "Error", "p": 3, "code": "C1083", "file": "pch.h", "line": 4, "project": PROJ,
     "msg": "Cannot open include file"},
    {"e": "ProjectFinished", "p": 3, "file": PROJ, "succeeded": False},
    {"e": "ProjectStarted", "p": 9, "ev": 7, "file": PROJ, "targets": "GetTargetPath",
     "parent": 3, "parentTask": 55, "global": {"Configuration": "Release"}},
    {"e": "BuildFinished", "succeeded": False},
]


def _log():
    return events.correlate(RECORDS)


def test_build_and_evaluation_facts():
    log = _log()
    assert log.schema == 1
    assert log.succeeded is False
    ev = log.evaluations[7]
    assert ev.file == PROJ
    assert ev.props.get("configurationtype") == "StaticLibrary"     # case-insensitive
    assert [i.spec for i in ev.items["ClCompile"]] == ["a.cpp", "pch.cpp"]
    assert ev.items["ClCompile"][1].get("precompiledheader") == "Create"
    assert ev.imports == ["C:\\work\\proj\\Directory.Build.props"]
    assert log.env_read == ["VCPKG_ROOT"]                           # deduplicated


def test_long_values_are_resolved_wherever_referenced():
    log = _log()
    assert log.evaluations[7].globals["Big"].startswith("<SolutionConfiguration>")
    assert log.projects[3].globals["Big"].startswith("<SolutionConfiguration>")


def test_project_target_task_tree():
    log = _log()
    proj = log.projects[3]
    assert (proj.file, proj.eval_id, proj.parent, proj.succeeded) == (PROJ, 7, -2, False)
    target = proj.target("ClCompile")
    assert target.succeeded is True
    (task,) = target.tasks
    assert task.name == "CL" and task.succeeded is True
    assert task.command_lines == ["CL.exe /c a.cpp"]
    assert [i.spec for i in task.inputs["Sources"]] == ["a.cpp", "pch.cpp"]
    assert task.scalar("TrackerLogDirectory") == "x64\\Release\\lib.tlog\\"
    assert [i.spec for i in task.outputs["Objs"]] == ["x64\\Release\\a.obj"]
    assert [t.name for t in proj.tasks("cl")] == ["CL"]              # names: any case
    assert log.evaluation_of(proj) is log.evaluations[7]


def test_items_added_by_a_target_body_are_kept():
    target = _log().projects[3].target("ClCompile")
    assert [i.spec for i in target.added["ClCompile"]] == ["gen\\module.g.cpp"]


def test_skipped_targets_keep_reason_and_declared_io():
    skipped = {s.name: s for s in _log().projects[3].skipped}
    up_to_date = skipped["GenerateVersionData"]
    assert up_to_date.reason == "OutputsUpToDate"
    assert up_to_date.inputs == ["C:\\work\\proj\\Version.props"]
    assert up_to_date.outputs == ["C:\\work\\proj\\src\\lib\\Generated Files\\version_gen.h"]
    assert skipped["Midl"].reason == "ConditionWasFalse"
    assert skipped["Midl"].inputs == []


def test_child_project_instances_link_to_their_parent():
    child = _log().projects[9]
    assert (child.parent, child.parent_task, child.entry_targets) == (3, 55, "GetTargetPath")


def test_errors_are_collected():
    (err,) = _log().errors
    assert (err.code, err.project) == ("C1083", PROJ)


def test_older_msbuild_logs_properties_on_project_started():
    log = events.correlate([
        {"e": "ProjectStarted", "p": 1, "file": PROJ, "targets": "", "parent": -2,
         "global": {}, "props": {"ConfigurationType": "Application"},
         "items": [{"t": "ClCompile", "i": "main.cpp", "m": {}}]},
    ])
    ev = log.evaluation_of(log.projects[1])
    assert ev.props.get("ConfigurationType") == "Application"
    assert [i.spec for i in ev.items["ClCompile"]] == ["main.cpp"]


def test_events_without_an_announced_owner_are_not_dropped():
    # A command line whose TaskStarted never arrived still lands on a task.
    log = events.correlate([
        {"e": "CommandLine", "p": 2, "t": 5, "k": 50, "task": "Link", "cmd": "link.exe /DLL"}])
    (task,) = list(log.projects[2].tasks("Link"))
    assert task.command_lines == ["link.exe /DLL"]


def test_load_reads_ndjson_and_completeness_check():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "events.ndjson")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for r in RECORDS:
                f.write(json.dumps(r) + "\n")
        assert events.load(path).projects[3].file == PROJ
        assert events._dump_is_complete(path)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(RECORDS[0]) + "\n")
        assert not events._dump_is_complete(path)        # replay died early


def test_filter_file_lists_properties_items_and_quiet_tasks():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "filter.txt")
        events._write_filter(path, ["OutDir", "Vcpkg*"], ["ClCompile"], ["Message"])
        with open(path, encoding="utf-8") as f:
            assert f.read().splitlines() == ["P OutDir", "P Vcpkg*", "I ClCompile", "X Message"]


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
