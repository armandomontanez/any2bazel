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

"""Extract a CanonicalModel from an MSBuild .binlog."""

import json
import os
import sys
import shlex
import re
from typing import Dict, List, Optional

from model import (Action, BuildSystem, CanonicalModel, Dependency, Target,
                   TargetKind, TargetRole)
from serialize import dump_model

def _split_command_line(cmd: str) -> List[str]:
    try:
        return shlex.split(cmd, posix=False)
    except ValueError:
        return cmd.split()

def _parse_csc_deps(args: List[str]) -> List[Dependency]:
    deps = []
    for arg in args:
        if arg.startswith("/reference:") or arg.startswith("-reference:") or \
           arg.startswith("/r:") or arg.startswith("-r:"):
            # e.g. /r:Foo.dll
            val = arg.split(":", 1)[1]
            # Strip quotes if any
            val = val.strip('"\'')
            name = os.path.splitext(os.path.basename(val))[0]
            deps.append(Dependency(name=name, external=True))
    return deps

def _parse_link_deps(args: List[str]) -> List[Dependency]:
    deps = []
    for arg in args:
        if not arg.startswith("/") and not arg.startswith("-"):
            if arg.lower().endswith(".lib"):
                name = os.path.splitext(os.path.basename(arg))[0]
                deps.append(Dependency(name=name, external=True))
    return deps

def _infer_kind(tasks: List[dict]) -> TargetKind:
    # Default to UNKNOWN, refine based on task args
    kind = TargetKind.UNKNOWN
    for t in tasks:
        args = _split_command_line(t.get("CommandLineArguments", ""))
        if t["Name"] == "Csc":
            for arg in args:
                if arg.lower() in ["/target:library", "-target:library", "/t:library", "-t:library"]:
                    return TargetKind.SHARED
                elif arg.lower() in ["/target:exe", "-target:exe", "/t:exe", "-t:exe", "/target:winexe", "-target:winexe"]:
                    return TargetKind.EXECUTABLE
            # default csc target is exe
            return TargetKind.EXECUTABLE
        elif t["Name"] == "Link":
            raw_cmd = t.get("CommandLineArguments", "").lower()
            is_lib = "lib.exe" in raw_cmd
            is_dll = any(a.lower() == "/dll" or a.lower() == "-dll" for a in args)
            if is_lib:
                return TargetKind.STATIC
            elif is_dll:
                return TargetKind.SHARED
            else:
                return TargetKind.EXECUTABLE
    return kind

def extract(binlog_json_path: str, repo_root: str) -> CanonicalModel:
    with open(binlog_json_path, "r", encoding="utf-8") as f:
        projects = json.load(f)

    model = CanonicalModel(build_system=BuildSystem.MSBUILD, repo_root=repo_root)

    for proj in projects:
        name = os.path.splitext(os.path.basename(proj["ProjectFile"]))[0]
        kind = _infer_kind(proj.get("Tasks", []))
        target = Target(name=name, kind=kind, role=TargetRole.PRODUCTION)

        for task in proj.get("Tasks", []):
            task_name = task["Name"]
            cmd_args = _split_command_line(task.get("CommandLineArguments", ""))
            if not cmd_args:
                continue

            # MSBuild tasks sometimes include the unquoted tool path which gets split by shlex
            exe_index = -1
            for i, arg in enumerate(cmd_args):
                if arg.lower().endswith(".exe") or arg.lower().endswith(".exe\"") or arg.lower().endswith(".exe'"):
                    exe_index = i
                    break
            if exe_index != -1:
                cmd_args = cmd_args[exe_index + 1:]

            if task_name == "ClCompile":
                srcs = [a for a in cmd_args if not a.startswith("/") and not a.startswith("-") and a.lower().endswith((".c", ".cpp", ".cxx", ".cc"))]
                if not srcs:
                    target.actions.append(Action(
                        mnemonic="CppCompile",
                        arguments=tuple(cmd_args)
                    ))
                else:
                    proj_dir = os.path.dirname(proj["ProjectFile"])
                    rel_proj_dir = os.path.relpath(proj_dir, repo_root).replace('\\', '/')
                    for src in srcs:
                        # normalize path
                        norm_src = os.path.normpath(os.path.join(rel_proj_dir, src)).replace('\\', '/')
                        new_args = [a for a in cmd_args if a not in srcs] + [norm_src]
                        target.actions.append(Action(
                            mnemonic="CppCompile",
                            arguments=tuple(new_args)
                        ))
            elif task_name == "Link":
                target.actions.append(Action(
                    mnemonic="CppLink",
                    arguments=tuple(cmd_args)
                ))
                for dep in _parse_link_deps(cmd_args):
                    if dep not in target.deps:
                        target.deps.append(dep)
            elif task_name == "Csc":
                proj_dir = os.path.dirname(proj["ProjectFile"])
                rel_proj_dir = os.path.relpath(proj_dir, repo_root).replace('\\', '/')
                new_args = []
                for a in cmd_args:
                    if not a.startswith("/") and not a.startswith("-") and a.lower().endswith(".cs"):
                        norm_src = os.path.normpath(os.path.join(rel_proj_dir, a)).replace('\\', '/')
                        new_args.append(norm_src)
                    else:
                        new_args.append(a)
                target.actions.append(Action(
                    mnemonic="CSharpCompile",
                    arguments=tuple(new_args)
                ))
                for dep in _parse_csc_deps(cmd_args):
                    if dep not in target.deps:
                        target.deps.append(dep)

        model.add(target)

    return model

if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: extract_msbuild.py <binlog_json> <repo_root> <out.json>")
    binlog_json, repo_root, out = sys.argv[1], os.path.abspath(sys.argv[2]), sys.argv[3]
    dump_model(extract(binlog_json, repo_root), out)
    print(f"wrote {out}")
