#!/usr/bin/env python3
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

"""Compare what a Bazel build of an MSBuild project PRODUCED with what the
reference build produced, for outputs the command-line diff cannot see.

    python3 check_outputs.py <msbuild.report.json> <workspace> --reference-root DIR
        [--bazel-bin DIR] [--dumpbin PATH] [--only xaml,pri,binaries]

`diff.py` compares command lines. Three kinds of output are made by tools whose
command line says little about the result, so they are compared themselves,
after building:

  xaml      what the XAML compilers wrote. WinUI: generated code, compiled
            markup (`.xbf`) and the processed `.xaml`. WPF: generated code,
            compiled markup (`.baml`) and the resources file that embeds it.
            Markup and resources byte for byte; code without the lines that
            record where and by which version it was generated.
  pri       resource indexes, by content: `makepri dump` of both, then every
            named resource with its candidates (qualifiers, type, value).
  binaries  native programs and DLLs: exported symbols, imported DLLs and
            functions, and the kinds of resources embedded (version
            information, icons, manifest). A module another program loads by
            name is only the same module if these agree.

The workspace is the one the emitters wrote into (their summaries say which
target became what); `--bazel-bin` defaults to `<workspace>/bazel-bin`.
Exit status 1 if anything differs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Set, Tuple

# Lines of generated code that say where or by what it was generated: the path
# of the markup (absolute for WinUI, relative to the output directory for WPF),
# the tool's version, and a comment only the .NET Framework flavour of the WPF
# compiler writes.
_PROVENANCE = ("#pragma checksum", "#line", "//     Runtime Version",
               "[System.CodeDom.Compiler.GeneratedCodeAttribute(")
_RESOURCE_TYPES = {1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG", 6: "STRING",
                   9: "ACCELERATOR", 10: "RCDATA", 11: "MESSAGETABLE", 12: "GROUP_CURSOR",
                   14: "GROUP_ICON", 16: "VERSION", 24: "MANIFEST"}


def code_lines(text: str) -> List[str]:
    """Generated code without its provenance lines."""
    return [line for line in text.splitlines() if not line.lstrip().startswith(_PROVENANCE)]


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _same_bytes(a: str, b: str) -> bool:
    with open(a, "rb") as f1, open(b, "rb") as f2:
        return f1.read() == f2.read()


# ---- XAML -------------------------------------------------------------------

def xaml_outputs(xaml: dict, target: str) -> List[Tuple[str, str]]:
    """(reference path, path below the package's output directory) of every
    file the XAML compiler wrote for a project, from the report's `xaml`
    digest and the name the emitter gave the target."""
    first = xaml["pass1"]
    out_dir = first["output_path"].rstrip("/")
    pairs: List[Tuple[str, str]] = []

    def add(path: str, stage: str) -> None:
        pairs.append((path, "%s_%s/%s" % (target, stage, path[len(out_dir) + 1:])))

    if xaml.get("framework") == "winui":
        for path in first.get("outputs", {}).get("code", []):
            add(path, "xaml1" if path.lower().endswith(".g.i.cs") else "xaml2")
        for kind in ("xaml", "xbf"):
            for path in xaml.get("pass2", {}).get("outputs", {}).get(kind, []):
                add(path, "xaml2")
    else:
        for kind in ("code", "baml"):
            for path in first.get("outputs", {}).get(kind, []):
                add(path, "wpf1")
        for path in xaml.get("pass2", {}).get("outputs", {}).get("baml", []):
            add(path, "wpf2")
        packed = (xaml.get("resources") or {}).get("output")
        if packed:
            pairs.append((packed, packed.rsplit("/", 1)[-1]))
    return pairs


def compare_file(mine: str, theirs: str) -> Optional[str]:
    """None if the two files agree, else what is different."""
    if not os.path.isfile(mine):
        return "not built"
    if not os.path.isfile(theirs):
        return "not in the reference tree"
    if mine.lower().endswith(".cs"):
        a, b = code_lines(_read_text(mine)), code_lines(_read_text(theirs))
        if a == b:
            return None
        first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        return "differs at line %d of %d (reference: %d lines)" % (first + 1, len(a), len(b))
    if _same_bytes(mine, theirs):
        return None
    return "different bytes (%d here, %d in the reference)" % (
        os.path.getsize(mine), os.path.getsize(theirs))


def check_xaml(report: dict, managed: dict, reference_root: str, bazel_bin: str) -> List[dict]:
    results = []
    for name, entry in sorted(report.get("targets", {}).items()):
        xaml = (entry.get("csharp") or {}).get("xaml") or {}
        record = managed.get(name, {})
        if "pass1" not in xaml or not record.get("xaml"):
            continue
        base = os.path.join(bazel_bin, record["package"].replace("/", os.sep))
        differences = {}
        pairs = xaml_outputs(xaml, record["target"])
        for reference, built in pairs:
            problem = compare_file(os.path.join(base, built.replace("/", os.sep)),
                                   os.path.join(reference_root, reference.replace("/", os.sep)))
            if problem:
                differences[built] = problem
        results.append({"target": name, "kind": xaml["framework"], "files": len(pairs),
                        "differences": differences})
    return results


# ---- resource indexes -----------------------------------------------------

def pri_resources(dump: ET.Element) -> Dict[str, List[Tuple[str, str, str]]]:
    """{resource path: sorted [(qualifiers, type, value)]} from the XML that
    `makepri dump /dt detailed` writes. Embedded data is its length and hash."""
    resources: Dict[str, List[Tuple[str, str, str]]] = {}

    def walk(node: ET.Element, prefix: str) -> None:
        for child in node:
            if child.tag == "ResourceMapSubtree":
                walk(child, prefix + child.get("name", "") + "/")
            elif child.tag == "NamedResource":
                candidates = []
                for candidate in child.iter("Candidate"):
                    qualifiers = ",".join(sorted("%s=%s" % (q.get("name"), q.get("value"))
                                                 for q in candidate.iter("Qualifier")))
                    value = candidate.find("Value")
                    embedded = candidate.find("Base64Value")
                    text = (value.text if value is not None else "") or ""
                    if embedded is not None:
                        text = "base64:%d:%d" % (len(embedded.text or ""), hash(embedded.text))
                    candidates.append((qualifiers, candidate.get("type") or "", text))
                resources[prefix + child.get("name", "")] = sorted(candidates)

    for resource_map in dump.iter("ResourceMap"):
        walk(resource_map, resource_map.get("name", "") + ":")
    return resources


def compare_resources(mine: dict, theirs: dict) -> Dict[str, List[str]]:
    out = {"only_here": sorted(set(mine) - set(theirs)),
           "only_in_reference": sorted(set(theirs) - set(mine)),
           "different": sorted(k for k in set(mine) & set(theirs) if mine[k] != theirs[k])}
    return {k: v for k, v in out.items() if v}


def _dump_pri(makepri: str, pri: str) -> Dict[str, List[Tuple[str, str, str]]]:
    with tempfile.TemporaryDirectory() as scratch:
        out = os.path.join(scratch, "dump.xml")
        done = subprocess.run([makepri, "dump", "/if", pri, "/of", out, "/dt", "detailed", "/o"],
                              capture_output=True, text=True, errors="replace")
        if not os.path.isfile(out):
            raise RuntimeError("makepri dump of %s failed: %s" % (pri, done.stdout[-300:]))
        return pri_resources(ET.parse(out).getroot())


def check_pri(report: dict, managed: dict, reference_root: str, bazel_bin: str) -> List[dict]:
    results = []
    for name, entry in sorted(report.get("targets", {}).items()):
        pri, record = entry.get("pri"), managed.get(name, {})
        if not pri or not record.get("pri"):
            continue
        mine = os.path.join(bazel_bin, record["package"].replace("/", os.sep),
                            pri["output"].rsplit("/", 1)[-1])
        theirs = os.path.join(reference_root, pri["output"].replace("/", os.sep))
        result: dict = {"target": name}
        if not os.path.isfile(mine):
            result["differences"] = {"file": "not built"}
        else:
            makepri = pri["makepri"].replace("/", os.sep)
            a, b = _dump_pri(makepri, mine), _dump_pri(makepri, theirs)
            result["resources"] = len(a)
            result["differences"] = compare_resources(a, b)
        results.append(result)
    return results


# ---- native binaries --------------------------------------------------------

def parse_exports(lines: Sequence[str]) -> Set[str]:
    """Exported names from `dumpbin /exports`."""
    names = set()
    for line in lines:
        m = re.match(r"^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]{8}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return names


def parse_imports(lines: Sequence[str]) -> Dict[str, Set[str]]:
    """{dll (lower case): functions} from `dumpbin /imports`."""
    result: Dict[str, Set[str]] = {}
    dll = None
    for line in lines:
        m = re.match(r"^    (\S+\.(?:dll|drv|exe|ocx))\s*$", line, re.IGNORECASE)
        if m:
            dll = m.group(1).lower()
            result.setdefault(dll, set())
            continue
        m = re.match(r"^\s+[0-9A-F]+\s+(\S+)\s*$", line)
        if m and dll:
            result[dll].add(m.group(1))
    return result


def resource_types(data: bytes) -> Dict[str, int]:
    """Top level of a PE file's resource directory: {type: entries}."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    optional = struct.unpack_from("<H", data, pe + 20)[0]
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    directory = pe + 24 + (112 if magic == 0x20B else 96) + 2 * 8
    rva = struct.unpack_from("<I", data, directory)[0]
    if not rva:
        return {}
    table = pe + 24 + optional
    for i in range(sections):
        virtual_size, address, raw_size, raw = struct.unpack_from("<IIII", data, table + 40 * i + 8)
        if address <= rva < address + max(virtual_size, raw_size):
            base = raw + rva - address
            break
    else:
        return {}

    def entries(offset: int) -> List[Tuple[int, int]]:
        named, ids = struct.unpack_from("<HH", data, base + offset + 12)
        return [struct.unpack_from("<II", data, base + offset + 16 + 8 * i) for i in range(named + ids)]

    found: Dict[str, int] = {}
    for name, child in entries(0):
        label = "named" if name & 0x80000000 else _RESOURCE_TYPES.get(name, str(name))
        found[label] = found.get(label, 0) + (len(entries(child & 0x7FFFFFFF))
                                              if child & 0x80000000 else 1)
    return found


def compare_binaries(mine: dict, theirs: dict) -> Tuple[List[str], List[str]]:
    """(differences, notes) between two {exports, imports, resources}
    descriptions.

    The same function imported from another DLL is a note, not a difference:
    Windows ships most of kernel32 a second time as API-set libraries
    (`api-ms-win-core-*.dll`, through `WindowsApp.lib`), a symbol is taken from
    whichever import library the linker reaches first, and both names load the
    same code. It does say the libraries were given in another order."""
    out: List[str] = []
    notes: List[str] = []
    a, b = mine["exports"], theirs["exports"]
    if a != b:
        out.append("exports: %d only here %s, %d only in the reference %s"
                   % (len(a - b), sorted(a - b)[:3], len(b - a), sorted(b - a)[:3]))
    ia, ib = mine["imports"], theirs["imports"]
    functions_a = {f for functions in ia.values() for f in functions}
    functions_b = {f for functions in ib.values() for f in functions}
    if functions_a != functions_b:
        out.append("imported functions: %d only here %s, %d only in the reference %s"
                   % (len(functions_a - functions_b), sorted(functions_a - functions_b)[:4],
                      len(functions_b - functions_a), sorted(functions_b - functions_a)[:4]))
    elif ia != ib:
        moved = sum(len(ia.get(d, set()) - ib.get(d, set())) for d in ia)
        notes.append("%d functions are imported from another DLL than in the reference "
                     "(only in the reference: %s): the link line has its libraries in another order"
                     % (moved, sorted(set(ib) - set(ia))[:3]))
    if mine["resources"] != theirs["resources"]:
        out.append("resources: %s here, %s in the reference" % (mine["resources"], theirs["resources"]))
    return out, notes


def _describe(dumpbin: str, path: str) -> dict:
    def run(option: str) -> List[str]:
        return subprocess.run([dumpbin, option, path], capture_output=True, text=True,
                              errors="replace").stdout.splitlines()
    with open(path, "rb") as f:
        data = f.read()
    return {"exports": parse_exports(run("/exports")), "imports": parse_imports(run("/imports")),
            "resources": resource_types(data), "size": len(data)}


def check_binaries(report: dict, native: dict, reference_root: str, bazel_bin: str,
                   dumpbin: str) -> List[dict]:
    results = []
    for name, entry in sorted(report.get("targets", {}).items()):
        record = native.get(name, {})
        if entry.get("kind") not in ("shared_library", "executable") or "label" not in record \
                or "unhandled" in record:
            continue
        package, target = record["label"][2:].split(":")
        mine = os.path.join(bazel_bin, package.replace("/", os.sep),
                            target + (".exe" if entry["kind"] == "executable" else ""))
        theirs = os.path.join(reference_root, entry["output"].replace("/", os.sep))
        result: dict = {"target": name}
        if not os.path.isfile(mine) or not os.path.isfile(theirs):
            result["differences"] = ["not built" if not os.path.isfile(mine)
                                     else "not in the reference tree"]
        else:
            a, b = _describe(dumpbin, mine), _describe(dumpbin, theirs)
            result.update(exports=len(a["exports"]), imports=len(a["imports"]),
                          size=a["size"], reference_size=b["size"])
            result["differences"], result["notes"] = compare_binaries(a, b)
        results.append(result)
    return results


def find_dumpbin(report: dict) -> Optional[str]:
    """dumpbin.exe of the toolset the reference build used."""
    for external in report.get("externals", []):
        if external.get("kind") == "msvc" and external.get("id") == "msvc":
            candidate = os.path.join(external["root"], "bin", "Hostx64", "x64", "dumpbin.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


# ---- report -------------------------------------------------------------------

def render(results: Dict[str, List[dict]]) -> Tuple[str, bool]:
    lines: List[str] = []
    failed = False
    for section, entries in results.items():
        bad = [e for e in entries if e.get("differences")]
        failed = failed or bool(bad)
        lines.append("%s: %d compared, %d differ" % (section, len(entries), len(bad)))
        for entry in entries:
            differences = entry.get("differences")
            detail = ", ".join("%s %s" % (v, k) for k, v in entry.items()
                               if k in ("files", "resources", "exports", "imports"))
            lines.append("  %-4s %s%s" % ("DIFF" if differences else "same", entry["target"],
                                         " (%s)" % detail if detail else ""))
            if isinstance(differences, dict):
                for key, value in list(differences.items())[:8]:
                    lines.append("         %s: %s" % (key, value if isinstance(value, str)
                                                      else "%d: %s" % (len(value), value[:4])))
            else:
                lines += ["         " + d for d in (differences or [])]
            lines += ["         note: " + n for n in entry.get("notes", [])]
    return "\n".join(lines), failed


def _summary(workspace: str, name: str) -> dict:
    try:
        with open(os.path.join(workspace, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report")
    parser.add_argument("workspace", help="the workspace emit_cc.py / emit_dotnet.py wrote into")
    parser.add_argument("--reference-root", required=True, help="tree the reference build ran in")
    parser.add_argument("--bazel-bin", help="default: <workspace>/bazel-bin")
    parser.add_argument("--dumpbin", help="default: the reference toolset's")
    parser.add_argument("--only", default="xaml,pri,binaries")
    args = parser.parse_args(argv)
    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    bazel_bin = args.bazel_bin or os.path.join(args.workspace, "bazel-bin")
    managed = _summary(args.workspace, "emit_dotnet.summary.json")
    native = _summary(args.workspace, "emit_cc.summary.json").get("targets", {})
    wanted = set(args.only.split(","))
    results: Dict[str, List[dict]] = {}
    if "xaml" in wanted:
        results["xaml"] = check_xaml(report, managed, args.reference_root, bazel_bin)
    if "pri" in wanted:
        results["pri"] = check_pri(report, managed, args.reference_root, bazel_bin)
    if "binaries" in wanted:
        dumpbin = args.dumpbin or find_dumpbin(report)
        if not dumpbin:
            print("binaries: no dumpbin.exe found; pass --dumpbin", file=sys.stderr)
            return 2
        results["binaries"] = check_binaries(report, native, args.reference_root, bazel_bin, dumpbin)
    text, failed = render(results)
    print(text)
    print("DIFFERENT" if failed else "SAME")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
