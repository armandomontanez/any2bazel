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

"""Compare, file by file, the generated sources a `source_map` token hides.

`ignore.source_map` collapses a tree of generated sources to one token on each
side, so that `diff.py` can say "both builds compile the projection". What it
cannot say is which files that tree holds or what is in them: Bazel only knows
the contents of a directory output after building it, and a command line names
the directory, not the files. A Bazel rule that generates one file too many, or
a generator that ran with different inputs, therefore diffs clean.

(Found the hard way on winget-cli: its MSBuild target leaves `Module.cs` out of
the C#/WinRT sources it compiles. The Bazel rule did not, every command-line
comparison converged, and the compile failed on a platform analyzer.)

This check closes the gap AFTER a build. For every token that has sources on
both sides it pairs the files -- by path below the mapped prefix, or one to one
when each side has a single file -- and compares their text. Encoding (BOM,
UTF-16), line endings and newlines at the end of the file are not differences:
the same text compiles the same.

It also compares, byte for byte, the resources both builds embed under the same
name in the same assembly: a `.resources` file is converted from `.resx` by
each build's own tool.

Usage:
    check_generated.py <reference model> <bazel model> <any2bazel.json>
                       [--reference-root DIR] [--bazel-root DIR] [--json FILE]

The roots default to each model's `repo_root`. Bazel paths are resolved through
the `bazel-out` link of the workspace, so the targets must have been built.
Exit status 1 if any pair differs, a file has no counterpart, or a generated
tree is not there to read.
"""

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import config as config_mod
from reconstruct import reconstruct
from serialize import load_model

_BAZEL_OUT = re.compile(r"^bazel-out/[^/]+/bin/")
# Lines of a difference shown per file.
_CONTEXT = 120


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _token_of(source: str, cfg) -> Optional[Tuple[str, str]]:
    """(token, matching prefix) if `source_map` collapses this source."""
    # Ignoring case, like diff.py pairs C# sources: these are Windows builds.
    source = cfg.map_source_dir(source, fold=True)
    low = source.lower()
    best = None
    for frm, to in cfg.source_map:
        if low.startswith(frm.lower()) and (best is None or len(frm) > len(best[0])):
            best = (frm, to)
    return (best[1], best[0]) if best else None


def collect(views: dict, cfg, bazel: bool) -> Dict[str, Dict[str, str]]:
    """token -> {path below the mapped prefix: source as the model has it} for
    one build. A source that IS the prefix (a directory output, or a single
    mapped file) has the empty string as its relative path."""
    out: Dict[str, Dict[str, str]] = {}
    for view in views.values():
        sources = [tu.source for tu in view.tus]
        for group in view.compile_groups:
            sources.extend(group.sources)
        for source in sources:
            written = _posix(source)
            probe = _BAZEL_OUT.sub("bazel-bin/", written) if bazel else written
            hit = _token_of(probe, cfg)
            if hit:
                token, prefix = hit
                out.setdefault(token, {})[probe[len(prefix):]] = written
    return out


def _expand(root: str, entries: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Files on disk for one side of a token: {relative path: absolute path},
    plus the entries that are not there. A directory stands for its files."""
    files: Dict[str, str] = {}
    absent: List[str] = []
    for relative, source in sorted(entries.items()):
        path = os.path.join(root, source.replace("/", os.sep))
        if os.path.isdir(path):
            for directory, _, names in os.walk(path):
                for name in names:
                    full = os.path.join(directory, name)
                    below = _posix(os.path.relpath(full, path))
                    files[(relative + "/" + below).strip("/") if relative else below] = full
        elif os.path.isfile(path):
            files[relative or os.path.basename(path)] = path
        else:
            absent.append(source)
    return files, absent


def read_text(path: str) -> Optional[str]:
    """The file as text with `\\n` line ends and no newline at the end, or
    None if it is not text."""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _first_difference(a: str, b: str) -> dict:
    left, right = a.split("\n"), b.split("\n")
    for number, (x, y) in enumerate(zip(left, right), 1):
        if x != y:
            return {"line": number, "reference": x[:_CONTEXT], "bazel": y[:_CONTEXT]}
    number = min(len(left), len(right)) + 1
    return {"line": number,
            "reference": (left[number - 1] if len(left) >= number else "<end of file>")[:_CONTEXT],
            "bazel": (right[number - 1] if len(right) >= number else "<end of file>")[:_CONTEXT]}


def _same(a: str, b: str) -> Tuple[bool, Optional[dict]]:
    ta, tb = read_text(a), read_text(b)
    if ta is None or tb is None:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return (fa.read() == fb.read()), None
    return (ta == tb), (None if ta == tb else _first_difference(ta, tb))


def compare(reference: Dict[str, Dict[str, str]], bazel: Dict[str, Dict[str, str]],
            reference_root: str, bazel_root: str) -> List[dict]:
    """One record per token: how the files behind it correspond."""
    results: List[dict] = []
    for token in sorted(set(reference) | set(bazel)):
        record = {"token": token, "identical": 0, "only_reference": [], "only_bazel": [],
                  "different": [], "not_found": []}
        if token not in reference or token not in bazel:
            # Which sources each build compiles is diff.py's finding; there is
            # nothing to pair here.
            record["status"] = "one_side_only"
            record["side"] = "reference" if token in reference else "bazel"
            results.append(record)
            continue
        a, a_absent = _expand(reference_root, reference[token])
        b, b_absent = _expand(bazel_root, bazel[token])
        record["not_found"] = a_absent + b_absent
        if len(a) == 1 and len(b) == 1:
            pairs = [(next(iter(a)), next(iter(a.values())), next(iter(b.values())))]
        else:
            folded = {k.lower(): k for k in b}
            pairs = []
            for key in sorted(a):
                if key.lower() in folded:
                    pairs.append((key, a[key], b[folded.pop(key.lower())]))
                else:
                    record["only_reference"].append(key)
            record["only_bazel"] = sorted(folded.values())
        for key, pa, pb in pairs:
            same, where = _same(pa, pb)
            if same:
                record["identical"] += 1
            else:
                record["different"].append(dict(where or {}, file=key))
        clean = not (record["only_reference"] or record["only_bazel"]
                     or record["different"] or record["not_found"])
        record["status"] = "identical" if clean else "mismatch"
        results.append(record)
    return results


def compare_resources(a_views: dict, b_views: dict, cfg, reference_root: str,
                      bazel_root: str) -> List[dict]:
    """One record per C# assembly both builds compile: the resources embedded
    under the same name, byte for byte. A `.resources` file is the output of a
    conversion (MSBuild's GenerateResource, a rule on the bazel side), and the
    command lines only say that both builds embed something called that."""
    from diff import _csharp_assemblies, _pair_assemblies
    names = lambda views: {n for n in views if not cfg.target_excluded(n)}
    pairs, _, _ = _pair_assemblies(_csharp_assemblies(a_views, names(a_views)),
                                   _csharp_assemblies(b_views, names(b_views)))
    results: List[dict] = []
    for a, b in pairs:
        if a.group.csc is None or b.group.csc is None:
            continue
        ours = {name: path for name, path in b.group.csc.resources}
        record = {"token": "resources of " + a.label, "identical": 0, "only_reference": [],
                  "only_bazel": [], "different": [], "not_found": []}
        for name, path in sorted(a.group.csc.resources):
            if name not in ours:
                continue            # a missing resource is diff.py's finding
            left = os.path.join(reference_root, _posix(path).replace("/", os.sep))
            right = os.path.join(bazel_root, _posix(ours[name]).replace("/", os.sep))
            missing = [p for p, full in ((path, left), (ours[name], right))
                       if not os.path.isfile(full)]
            if missing:
                record["not_found"] += missing
                continue
            with open(left, "rb") as fa, open(right, "rb") as fb:
                if fa.read() == fb.read():
                    record["identical"] += 1
                else:
                    record["different"].append({"file": name})
        if record["identical"] or record["different"] or record["not_found"]:
            record["status"] = "mismatch" if record["different"] or record["not_found"]                 else "identical"
            results.append(record)
    return results


def failed(results: List[dict]) -> bool:
    return any(r["status"] == "mismatch" for r in results)


def render(results: List[dict]) -> str:
    lines: List[str] = []
    for r in results:
        if r["status"] == "one_side_only":
            lines.append("[skip] %s: compiled on the %s side only (see diff.py)"
                         % (r["token"], r["side"]))
            continue
        lines.append("[%s] %s: %d file(s) identical" % (
            "ok" if r["status"] == "identical" else "MISMATCH", r["token"], r["identical"]))
        for label, key in (("only in the reference", "only_reference"),
                           ("only in bazel", "only_bazel"),
                           ("not on disk (not built?)", "not_found")):
            for name in r[key][:10]:
                lines.append("    %s: %s" % (label, name))
            if len(r[key]) > 10:
                lines.append("    ... %d more %s" % (len(r[key]) - 10, label))
        for d in r["different"][:10]:
            lines.append("    differs: %s" % d["file"])
            if "line" in d:
                lines.append("      line %d  reference: %s" % (d["line"], d["reference"]))
                lines.append("      %s  bazel:     %s" % (" " * len("line %d" % d["line"]), d["bazel"]))
        if len(r["different"]) > 10:
            lines.append("    ... %d more files differ" % (len(r["different"]) - 10))
    checked = [r for r in results if r["status"] != "one_side_only"]
    bad = [r for r in checked if r["status"] == "mismatch"]
    lines.append("%s  generated trees checked=%d  mismatched=%d  files identical=%d" % (
        "MISMATCH" if bad else "IDENTICAL", len(checked), len(bad),
        sum(r["identical"] for r in checked)))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("reference_model")
    parser.add_argument("bazel_model")
    parser.add_argument("config")
    parser.add_argument("--reference-root")
    parser.add_argument("--bazel-root")
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args(argv)

    reference, bazel = load_model(args.reference_model), load_model(args.bazel_model)
    cfg = config_mod.load(args.config)
    a_views, b_views = reconstruct(reference), reconstruct(bazel)
    roots = (args.reference_root or reference.repo_root, args.bazel_root or bazel.repo_root)
    results = compare(collect(a_views, cfg, bazel=False), collect(b_views, cfg, bazel=True),
                      *roots)
    results += compare_resources(a_views, b_views, cfg, *roots)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    print(render(results))
    return 1 if failed(results) else 0


if __name__ == "__main__":
    sys.exit(main())
