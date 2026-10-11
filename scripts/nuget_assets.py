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

"""A NuGet lock for the Bazel side, written from what MSBuild resolved.

    python3 nuget_assets.py <msbuild.report.json> <repo_root> <nuget_lock.json>
        [--fetch-packs] [--pack-cache DIR] [--toolchain-packs ID,ID]

NuGet's resolution for a project (which version of which package, and which of
its files apply to the project's target framework) is the result of a
dependency solve plus asset-selection rules. Re-deriving it on the Bazel side
gives a different answer whenever the two implementations disagree -- and
rules_dotnet's package rule, for one, does not know platform frameworks such as
`net8.0-windows10.0.19041.0` and silently drops assets filed under them.

So this script does not resolve anything. It records the answer:

  * which package FILES each compile used -- from the csc command lines in the
    report (references, analyzers, additional files, sources);
  * which files each PROGRAM needs at run time -- the package files MSBuild
    copied beside it (the build's copy steps, in the report), together with
    the run-time and native assets of the project's `obj/project.assets.json`.
    A library gets none: MSBuild deploys a program's own restore graph;
  * the package hashes and the feeds -- from that assets file.

The lock has one entry per package (id, version, integrity) with its asset
selections, and one entry per C# target of the build naming the selection it
takes from each package it uses. A selection is named after the target
framework; when targets of one framework take different files from a package,
the less common selections are numbered (`<framework>_alt1`). Merging them
would hand a target references its reference build never passed, and a
reference is enough to change what a name binds to.
`nuget_assets.bzl` (see the example workspaces) turns the lock
into a repository of `import_library` targets, downloaded by integrity.

Two more things a compile reads are not in any project's restore graph:

  * FRAMEWORK PACKS on the machine (`dotnet\\packs\\Microsoft.WindowsDesktop.App.Ref`,
    the .NET Framework targeting pack). Each is a copy of a NuGet package and
    is locked as that package; `--fetch-packs` downloads one that no package
    folder has, and every file the build read is compared with the package's.
    The packs the Bazel rule set brings itself (`--toolchain-packs`) are left
    out, except for a target only the exact-command-line rule can compile.
  * A reference that DISPLACES an assembly of the toolchain's pack (the real
    `WindowsBase.dll` instead of the base pack's facade) is marked under
    `overrides` in its selection, because rules_dotnet has to be told.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from msbuild_csharp import classify_external, rules_dotnet_gaps

LOCK_SCHEMA = 1
_PLACEHOLDER = "_._"
# Framework packs rules_dotnet supplies for the target framework by itself.
TOOLCHAIN_PACKS = ("microsoft.netcore.app.ref", "netstandard.library")
NUGET_ORG = "https://api.nuget.org/v3/index.json"
# The package the WinUI XAML compiler ships in (lower case, as a lock key starts).
XAML_COMPILER_PACKAGE = "microsoft.windowsappsdk.winui"
_TOOL = re.compile(r'^\s*(?:"([^"]+)"|(\S+))')
# An absolute Windows path in a line of a config file, up to a list separator.
_CONFIG_PATH = re.compile(r"[A-Za-z]:[\\/][^|;,\"<>*?\r\n]+")
# An absolute Windows path in a response file, quoted or not.
_RSP_PATH = re.compile(r'"[A-Za-z]:[\\/][^"]+"|(?<![\w"])[A-Za-z]:[\\/]\S+')


def _key(package_id: str, version: str) -> str:
    return "%s/%s" % (package_id.lower(), version)


def framework_name(tfm: str, rid: str = "") -> str:
    """Name of a package's asset selection for one target framework (and
    runtime identifier): what the lock keys on and the Bazel target is called."""
    return "%s_%s" % (tfm, rid) if rid else tfm


def _assets_target(assets: dict, tfm: str, rid: str = "") -> Optional[dict]:
    """The `targets` entry of an assets file for a target framework. Newer
    NuGet keys targets by the project's own spelling (`net8.0-windows...`),
    older by the long name; `project.frameworks[*].targetAlias` links the two."""
    targets = assets.get("targets", {})
    wanted = [tfm]
    for name, fw in assets.get("project", {}).get("frameworks", {}).items():
        if fw.get("targetAlias", "").lower() == tfm.lower() or name.lower() == tfm.lower():
            wanted.append(name)
    bases = [k for k in targets if "/" not in k]
    for candidate in wanted:
        for key in targets:
            base, _, key_rid = key.partition("/")
            if base.lower() == candidate.lower() and key_rid.lower() == rid.lower():
                return targets[key]
    if not rid and len(bases) == 1:
        return targets[bases[0]]
    return None


def _files(section: Optional[dict]) -> List[str]:
    return sorted(f for f in (section or {}) if not f.endswith(_PLACEHOLDER))


def _integrity(library: Optional[dict], folders: Sequence[str], package_id: str,
               version: str) -> str:
    """`sha512-<base64>` of the package FILE a feed serves.

    NuGet keeps two hashes. `<id>.<version>.nupkg.sha512` beside an installed
    package is the hash of the .nupkg as downloaded. The `sha512` in an assets
    file (and `contentHash` in `.nupkg.metadata`) is computed with the
    package's signature removed, so for a signed package -- everything from
    nuget.org -- it is NOT the hash of the file, and a download checked
    against it fails. The file hash is used; the content hash only when the
    package is no longer on disk (correct for unsigned packages only)."""
    name = "%s.%s.nupkg" % (package_id.lower(), version.lower())
    for folder in folders:
        directory = os.path.join(folder, package_id.lower(), version.lower())
        try:
            with open(os.path.join(directory, name + ".sha512"), encoding="utf-8") as f:
                digest = f.read().strip()
            if digest:
                return "sha512-" + digest
        except OSError:
            pass
        try:
            hasher = hashlib.sha512()
            with open(os.path.join(directory, name), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    hasher.update(chunk)
            return "sha512-" + base64.b64encode(hasher.digest()).decode("ascii")
        except OSError:
            pass
    if library and library.get("sha512"):
        return "sha512-" + library["sha512"]
    return ""


def _file_integrity(path: str) -> str:
    hasher = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return "sha512-" + base64.b64encode(hasher.digest()).decode("ascii")


def _posix_name(path: str) -> str:
    return path.replace(chr(92), "/").rsplit("/", 1)[-1]


def _below(path: str, directory: str) -> bool:
    return bool(directory) and path.replace("\\", "/").lower().startswith(directory.lower() + "/")


def _deployed_as(destination: str, out_dir: str, package_path: str) -> Optional[str]:
    """How a package file MSBuild copied to `destination` is needed at run
    time: `runtime` (an assembly beside the program), `resources` (a satellite
    assembly in its culture directory), `native`, or None if it was not
    deployed with the program."""
    if not _below(destination, out_dir):
        return None
    relative = destination.replace("\\", "/")[len(out_dir) + 1:]
    low = relative.lower()
    # Assemblies only. A package's own launcher (testhost.exe beside
    # testhost.dll) would be a second assembly of the same name to the host.
    if not low.endswith(".dll"):
        return None
    if "/" not in relative:
        native = "/native/" in "/" + package_path.lower()
        return "native" if native else "runtime"
    if relative.count("/") == 1 and low.endswith(".resources.dll"):
        return "resources"
    return None


def pack_package(pack_key: str, folders: Sequence[str]) -> Tuple[str, str, str]:
    """(package id, version, path prefix inside the package) of the NuGet
    package that a framework pack on the machine is a copy of.

    An SDK pack (`dotnet\\packs\\<Id>\\<version>\\`) is its package unpacked.
    The .NET Framework targeting pack Visual Studio installs has no version of
    its own; the same assemblies ship as
    `Microsoft.NETFramework.ReferenceAssemblies.net48` and its siblings."""
    pack_id, _, version = pack_key.partition("/")
    if pack_id.lower() == "microsoft.netframework.referenceassemblies":
        package_id = pack_id.lower() + ".net" + version.lstrip("vV").replace(".", "")
        known = []
        for folder in folders:
            try:
                known += os.listdir(os.path.join(folder, package_id))
            except OSError:
                pass
        newest = max(known, key=lambda v: [int(p) if p.isdigit() else 0
                                           for p in re.split(r"[.\-]", v)]) if known else "1.0.3"
        return package_id, newest, "build/.NETFramework/%s/" % version
    return pack_id.lower(), version, ""


def _package_bytes(folders: Sequence[str], package_id: str, version: str, relative: str,
                   archive: Optional[str]) -> Optional[bytes]:
    """A file of a package: from a package folder, or from the .nupkg."""
    for folder in folders:
        try:
            with open(os.path.join(folder, package_id.lower(), version.lower(),
                                   relative.replace("/", os.sep)), "rb") as f:
                return f.read()
        except OSError:
            pass
    if archive:
        try:
            with zipfile.ZipFile(archive) as z:
                wanted = relative.lower()
                for member in z.namelist():
                    if urllib.parse.unquote(member).lower() == wanted:
                        return z.read(member)
        except (OSError, zipfile.BadZipFile):
            pass
    return None


def verify_pack(root: str, prefix: str, files: Iterable[str], folders: Sequence[str],
                package_id: str, version: str, archive: Optional[str]) -> dict:
    """Compare the pack files a build read with the same files of the package
    that will stand in for them: {"identical": n, "different": [...],
    "unverified": [...]} (unverified: one of the two copies is not readable)."""
    result = {"identical": 0, "different": [], "unverified": []}
    for relative in sorted(set(files)):
        try:
            with open(os.path.join(root, relative.replace("/", os.sep)), "rb") as f:
                machine = f.read()
        except OSError:
            machine = None
        packaged = _package_bytes(folders, package_id, version, prefix + relative, archive)
        if machine is None or packaged is None:
            result["unverified"].append(relative)
        elif machine == packaged:
            result["identical"] += 1
        else:
            result["different"].append(relative)
    return result


def build_lock(report: dict, repo_root: str,
               read_assets=None, package_folders: Optional[Sequence[str]] = None,
               toolchain_packs: Sequence[str] = TOOLCHAIN_PACKS, fetch=None,
               verify: bool = True, list_directory=None, read_text=None) -> dict:
    """The lock for every C# target in `report`. `read_assets(path)` returns a
    parsed assets file or None (tests inject it); `package_folders` are extra
    NuGet package folders to look for hashes in.

    Framework packs the build read from the machine are locked as the NuGet
    packages they are, except `toolchain_packs` (prefixes of pack ids the Bazel
    rule set supplies itself). `fetch(package id, version)` downloads such a
    package when no package folder has it and returns (path of the .nupkg, feed
    it came from) or None; without it the package is reported unresolved."""
    if read_text is None:
        def read_text(path: str) -> Optional[str]:
            try:
                with open(path, encoding="utf-8-sig", errors="replace") as f:
                    return f.read()
            except OSError:
                return None
    if list_directory is None:
        def list_directory(path: str) -> List[str]:
            try:
                return os.listdir(path)
            except OSError:
                return []
    if read_assets is None:
        def read_assets(path: str) -> Optional[dict]:
            try:
                with open(path, encoding="utf-8-sig") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None

    packages: Dict[str, dict] = {}
    targets: Dict[str, dict] = {}
    sources: List[str] = []
    unresolved: List[str] = []
    folders: List[str] = list(package_folders or [])
    proper_id: Dict[str, str] = {}
    libraries: Dict[str, dict] = {}
    cache: Dict[str, Optional[dict]] = {}
    pending_packs: List[tuple] = []

    def package(package_id: str, version: str) -> dict:
        key = _key(package_id, version)
        if key not in packages:
            packages[key] = {"id": proper_id.get(key, package_id), "version": version,
                             "integrity": "", "frameworks": {}}
        return packages[key]

    def selection(entry: dict, package_id: str, version: str) -> dict:
        """What one target takes from one package."""
        package(package_id, version)
        return entry["packages"].setdefault(
            _key(package_id, version),
            {"compile": set(), "runtime": set(), "native": set(), "analyzers": set(),
             "resources": set()})

    for target_name in sorted(report.get("targets", {})):
        digest = report["targets"][target_name].get("csharp")
        if not digest:
            continue
        tfm = digest.get("target_framework", "")
        rid = digest.get("runtime_identifier", "")
        name = framework_name(tfm, rid)
        entry = {"framework": name, "target_framework": tfm, "packages": {}, "files": {},
                 "deployed": {}, "displaced": [], "packs": {}, "_exact": False}
        targets[target_name] = entry
        # A target rules_dotnet's own rules cannot reproduce is compiled from
        # the lock alone, so it takes even the toolchain's packs from here.
        project = report["targets"][target_name].get("project", "")
        exact = bool(rules_dotnet_gaps(digest, project.replace("\\", "/").rsplit("/", 1)[0]
                                       if "/" in project.replace("\\", "/") else ""))
        entry["_exact"] = exact
        for role, section in (("compile", digest.get("references", {})),
                              ("analyzers", digest.get("analyzers", {}))):
            for pack_key, files in section.get("pack_files", {}).items():
                if exact or not pack_key.lower().startswith(tuple(toolchain_packs)):
                    pending_packs.append((entry, role, pack_key, files,
                                          section.get("pack_roots", {}).get(pack_key, "")))

        assets = None
        asset_compile: Dict[str, Set[str]] = {}     # package -> its NuGet compile assets here
        if digest.get("assets_file"):
            path = os.path.join(repo_root, digest["assets_file"].replace("/", os.sep))
            if path not in cache:
                cache[path] = read_assets(path)
            assets = cache[path]
        if assets:
            for folder in assets.get("packageFolders", {}):
                if folder not in folders:
                    folders.append(folder)
            for source in assets.get("project", {}).get("restore", {}).get("sources", {}):
                if source.lower().startswith(("http://", "https://")) and source not in sources:
                    sources.append(source)
            for lib_key, lib in assets.get("libraries", {}).items():
                if lib.get("type") == "package":
                    package_id, _, version = lib_key.partition("/")
                    proper_id.setdefault(_key(package_id, version), package_id)
                    libraries.setdefault(_key(package_id, version), lib)
            # Run-time closure: every package of the project's resolved graph
            # that ships something to load -- unless the build displaced it.
            # A package whose assembly the target framework already contains
            # (Microsoft.Win32.Registry on .NET 8) loses MSBuild's conflict
            # resolution: its reference is not on the compile line and its
            # run-time copy is not deployed. Depending on it from Bazel would
            # make rules_dotnet drop the framework's assembly in its favour.
            compiled_from = {_key(i["id"], i["version"])
                             for i in digest.get("references", {}).get("nuget", [])}
            resolved = _assets_target(assets, tfm, rid) or _assets_target(assets, tfm) or {}
            for lib_key, lib in resolved.items():
                if lib.get("type") != "package":
                    continue
                package_id, _, version = lib_key.partition("/")
                asset_compile[_key(package_id, version)] = {
                    f.lower() for f in _files(lib.get("compile"))}
                if _files(lib.get("compile")) and _key(package_id, version) not in compiled_from:
                    entry["displaced"].append(_key(package_id, version))
                    continue
                runtime, native = _files(lib.get("runtime")), _files(lib.get("native"))
                if runtime or native:
                    chosen = selection(entry, package_id, version)
                    chosen["runtime"].update(runtime)
                    chosen["native"].update(native)

        # Compile closure: exactly the package files the compiler was given.
        for role, group in (("compile", digest.get("references", {}).get("nuget", [])),
                            ("analyzers", digest.get("analyzers", {}).get("nuget", []))):
            for item in group:
                chosen = selection(entry, item["id"], item["version"])
                chosen[role].add(item["path"])
                # An assembly that is NOT one of the package's NuGet compile
                # assets got on the command line as a `<Reference>` from the
                # package's own .props (MSTest's adapter, under `build/`).
                # MSBuild copies such a reference beside the program, and the
                # program loads it; the assets file's run-time list does not
                # have it.
                known = asset_compile.get(_key(item["id"], item["version"]))
                path = item["path"].lower()
                if role == "compile" and known is not None and path not in known \
                        and path.endswith((".dll", ".exe")) and not path.startswith("ref/"):
                    chosen["runtime"].add(item["path"])
        loose = (digest.get("additional_files", []) + digest.get("analyzer_configs", [])
                 + digest.get("sources", {}).get("external", []))
        output = report["targets"][target_name].get("output", "").replace("\\", "/")
        out_dir = output.rsplit("/", 1)[0] if "/" in output else ""
        program = digest.get("target", "library") != "library"
        # Package files MSBuild's generated analyzer config names by path (the
        # metadata a source generator is pointed at).
        for config in digest.get("analyzer_configs", []):
            if config.get("kind") == "generated":
                text = read_text(os.path.join(repo_root, config["path"].replace("/", os.sep)))
                for token in _CONFIG_PATH.findall(text or ""):
                    named = classify_external(token)
                    if named and named.get("kind") == "nuget":
                        loose.append(named)
        # Tools a build step ran straight out of a package (cswinrt.exe).
        for step in report["targets"][target_name].get("steps", []):
            if step.get("kind") == "exec":
                m = _TOOL.match(step.get("command", ""))
                tool = classify_external(m.group(1) or m.group(2)) if m else None
                if tool and tool.get("kind") == "nuget":
                    loose.append(tool)
            elif step.get("kind") == "write" and step.get("lines"):
                # Package files a response file hands to a tool (metadata a
                # package ships, as input to a projection generator).
                for token in _RSP_PATH.findall(" ".join(step["lines"])):
                    named = classify_external(token.strip('"'))
                    if named and named.get("kind") == "nuget":
                        loose.append(named)
            elif step.get("kind") == "copy":
                copied_from = step.get("from", [])
                copied_to = step.get("to", [])
                if isinstance(copied_to, str):          # a destination folder
                    copied_to = [copied_to.rstrip("/") + "/" + _posix_name(src) for src in copied_from]
                for source, destination in zip(copied_from, copied_to):
                    copied = classify_external(source)
                    if not copied or copied.get("kind") not in ("nuget", "pack"):
                        continue
                    role = _deployed_as(destination, out_dir, copied["path"])
                    if program and _below(destination, out_dir):
                        # Whatever a rule makes of it, it is part of the
                        # program's directory: addressable as a file.
                        if copied["kind"] == "nuget":
                            package(copied["id"], copied["version"])
                            entry["deployed"].setdefault(
                                _key(copied["id"], copied["version"]), set()).add(copied["path"])
                        else:
                            key = "%s/%s" % (copied["id"], copied["version"])
                            root = source.replace("\\", "/")
                            root = root[:-len(copied["path"]) - 1] if root.lower().endswith(
                                "/" + copied["path"].lower()) else ""
                            pending_packs.append((entry, "deployed", key, [copied["path"]], root))
                    if role is None:
                        # Copied somewhere else in the project (sources a
                        # package embeds into its consumers, for one), or laid
                        # out in a subdirectory the Bazel rules have no place for.
                        if copied["kind"] == "nuget" and not _below(destination, out_dir):
                            loose.append(copied)
                    elif copied["kind"] == "nuget":
                        # What MSBuild put beside the program is what the
                        # program needs: more than the assets file's run-time
                        # list (files a package's .targets copy, such as
                        # MSTest's platform services).
                        selection(entry, copied["id"], copied["version"])[role].add(copied["path"])
                    elif exact or not ("%s/%s" % (copied["id"], copied["version"])).lower().startswith(
                            tuple(toolchain_packs)):
                        # The run-time half of a framework reference
                        # (WinRT.Runtime.dll of the Windows SDK projection).
                        key = "%s/%s" % (copied["id"], copied["version"])
                        root = source.replace("\\", "/")
                        root = root[:-len(copied["path"]) - 1] if root.lower().endswith(
                            "/" + copied["path"].lower()) else ""
                        pending_packs.append((entry, role, key, [copied["path"]], root))
        for item in loose:
            if item.get("kind") == "nuget":
                package(item["id"], item["version"])
                entry["files"].setdefault(_key(item["id"], item["version"]), set()).add(item["path"])
        # The template of the program's host executable.
        template = (digest.get("apphost") or {}).get("template", "")
        host = classify_external(template) if template else None
        if host and host.get("kind") == "pack":
            pending_packs.append((entry, None, "%s/%s" % (host["id"], host["version"]),
                                  [host["path"]], template[:-len(host["path"]) - 1]))
        elif host and host.get("kind") == "nuget":
            package(host["id"], host["version"])
            entry["files"].setdefault(_key(host["id"], host["version"]), set()).add(host["path"])

    # Framework packs read from the machine (WindowsDesktop, ASP.NET, the .NET
    # Framework reference assemblies): the same files, from the package.
    checks: Dict[str, dict] = {}
    for entry, role, pack_key, files, root in pending_packs:
        package_id, version, prefix = pack_package(pack_key, folders)
        key = _key(package_id, version)
        if role is None or role == "deployed":
            # A file of the pack some rule names by itself.
            package(package_id, version)
            entry["files" if role is None else "deployed"].setdefault(key, set()).update(
                prefix + f for f in files)
        else:
            selection(entry, package_id, version)[role].update(prefix + f for f in files)
        entry["packs"][pack_key] = key
        check = checks.setdefault(key, {"of": pack_key, "root": root, "prefix": prefix,
                                        "files": set()})
        check["files"].update(files)
        check["root"] = check["root"] or root

    # Assemblies of the toolchain's pack that MSBuild did NOT pass because
    # another reference displaced them: `WindowsBase.dll` of
    # Microsoft.NETCore.App.Ref is a facade, and a project that references
    # WindowsDesktop gets the real one instead. rules_dotnet passes its whole
    # pack unless a direct dependency carries the assembly's name, so the
    # displacing files are marked: the repository rule gives each a target of
    # that name.
    universe: Dict[str, Set[str]] = {}
    for target_name in targets:
        for pack_key, files in report["targets"][target_name]["csharp"].get(
                "references", {}).get("pack_files", {}).items():
            universe.setdefault(pack_key, set()).update(files)
    for target_name, entry in targets.items():
        if entry.pop("_exact"):
            continue        # compiled from the lock alone: nothing to displace
        references = report["targets"][target_name]["csharp"].get("references", {})
        dropped: Set[str] = set()
        for pack_key, files in references.get("pack_files", {}).items():
            if pack_key.lower().startswith(tuple(toolchain_packs)):
                whole = set(universe[pack_key])
                root = references.get("pack_roots", {}).get(pack_key)
                directories = {f.rsplit("/", 1)[0] for f in files if "/" in f}
                for directory in directories if root else ():
                    whole.update(directory + "/" + name
                                 for name in list_directory(root + "/" + directory))
                dropped |= {f.rsplit("/", 1)[-1].lower() for f in whole - set(files)
                            if f.lower().endswith(".dll")}
        for chosen in entry["packages"].values():
            displacing = {f for f in chosen["compile"] if f.rsplit("/", 1)[-1].lower() in dropped}
            if displacing:
                chosen["overrides"] = displacing

    for key in sorted(packages):
        record = packages[key]
        record["id"] = proper_id.get(key, record["id"])
        record["integrity"] = _integrity(libraries.get(key), folders, record["id"],
                                         record["version"])
        archive = None
        if not record["integrity"] and key in checks and fetch is not None:
            fetched = fetch(record["id"], record["version"])
            if fetched:
                archive, feed = fetched
                record["integrity"] = _file_integrity(archive)
                if feed and feed not in sources:
                    sources.append(feed)
        if not record["integrity"]:
            unresolved.append(key)
        if key in checks:
            check = checks[key]
            record["pack"] = {"of": check["of"]}
            if verify and check["root"]:
                record["pack"].update(verify_pack(check["root"], check["prefix"], check["files"],
                                                  folders, record["id"], record["version"], archive))

    # What a program loads is decided by the program's own project: MSBuild
    # deploys the files of ITS restore graph beside it, and a library deploys
    # nothing. So only programs keep run-time files. A library that carried
    # its own selection of a package's run-time files would add them to every
    # program that uses it -- or, since rules_dotnet keys run-time libraries by
    # package name and version, replace the program's selection with its own.
    for target_name, entry in targets.items():
        if report["targets"][target_name]["csharp"].get("target", "library") == "library":
            for chosen in entry["packages"].values():
                for role in ("runtime", "native", "resources"):
                    chosen[role] = set()
            entry["packages"] = {key: chosen for key, chosen in entry["packages"].items()
                                 if any(chosen.values())}

    # Two targets of one framework need not take the same files from a package
    # (a project that embeds the C#/WinRT runtime references the package's host
    # shim but not its runtime assembly). Each distinct selection is a variant:
    # the most used one is named after the framework, the others numbered.
    uses: Dict[Tuple[str, str], Dict[str, int]] = {}
    frozen: Dict[str, dict] = {}
    for entry in targets.values():
        for key, chosen in entry["packages"].items():
            exact = {role: sorted(files) for role, files in sorted(chosen.items()) if files}
            text = json.dumps(exact, sort_keys=True)
            frozen[text] = exact
            counts = uses.setdefault((key, entry["framework"]), {})
            counts[text] = counts.get(text, 0) + 1
            entry["packages"][key] = text
    variant: Dict[Tuple[str, str, str], str] = {}
    for (key, name), counts in sorted(uses.items()):
        ranked = sorted(counts, key=lambda text: (-counts[text], text))
        for position, text in enumerate(ranked):
            label = name if position == 0 else "%s_alt%d" % (name, position)
            variant[(key, name, text)] = label
            packages[key]["frameworks"][label] = frozen[text]
    for key in packages:
        packages[key]["frameworks"] = dict(sorted(packages[key]["frameworks"].items()))
    for entry in targets.values():
        entry["displaced"] = sorted(set(entry["displaced"]) - set(entry["packages"]))
        entry["packages"] = {key: variant[(key, entry["framework"], text)]
                             for key, text in sorted(entry["packages"].items())}
        entry["files"] = {k: sorted(v) for k, v in sorted(entry["files"].items())}
        # Package files MSBuild copied beside a program, whatever their role:
        # what a rule that lays the program's directory out takes by name.
        entry["deployed"] = {k: sorted(v) for k, v in sorted(entry["deployed"].items())}
        if not entry["deployed"]:
            del entry["deployed"]

    # Resource indexes: makepri with what it loads, and the indexes and images
    # packages contribute.
    for target_name, entry in targets.items():
        pri = report["targets"][target_name].get("pri")
        if not pri:
            continue
        paths = [pri.get("makepri", "")] + pri.get("pri_files", []) \
            + [f["path"] for f in pri.get("layout", [])]
        tool = pri.get("makepri", "")
        if "/" in tool:
            paths.append(tool.rsplit("/", 1)[0] + "/mrmsupport.dll")
        for path in paths:
            where = classify_external(path) if re.match(r"^[A-Za-z]:", path) else None
            if where and where.get("kind") == "nuget":
                key = _key(where["id"], where["version"])
                package(where["id"], where["version"])
                entry["files"][key] = sorted(set(entry["files"].get(key, [])) | {where["path"]})

    # The WinUI XAML compiler: a program of the package, with what it loads.
    for target_name, entry in targets.items():
        digest = report["targets"][target_name].get("csharp") or {}
        if (digest.get("xaml") or {}).get("framework") != "winui":
            continue
        for key in entry.get("packages", {}):
            if not key.startswith(XAML_COMPILER_PACKAGE + "/"):
                continue
            root = next((folder for folder in (
                os.path.join(base, key.replace("/", os.sep)) for base in folders)
                if list_directory(folder)), None)
            if root is None:
                unresolved.append(key + " (XAML compiler files)")
                continue
            tools = ["tools/net472/" + f for f in sorted(list_directory(os.path.join(root, "tools", "net472")))]
            tools += ["tools/%s/GenXbf.dll" % arch for arch in ("x64", "x86")
                      if "GenXbf.dll" in list_directory(os.path.join(root, "tools", arch))]
            entry["files"][key] = sorted(set(entry["files"].get(key, [])) | set(tools))

    for key, native in native_packages(report).items():
        record = packages.setdefault(key, {"id": native["id"], "version": native["version"],
                                           "integrity": "", "frameworks": {}})
        record["integrity"] = record["integrity"] or native["integrity"]
        if not record["integrity"]:
            unresolved.append(key)
        if native["cc"]:
            record["cc"] = native["cc"]
            # Which targets compile or link against the package: nothing else
            # in a target's entry says so when it takes no single file from it.
            for target_name in native["consumers"]:
                entry = targets.setdefault(target_name, {"files": {}})
                entry["cc"] = sorted(set(entry.get("cc", [])) | {key})
        for target_name, files in native["files"].items():
            entry = targets.setdefault(target_name, {"files": {}})
            entry["files"][key] = sorted(set(entry["files"].get(key, [])) | set(files))
        if native["source"] and native["source"] not in sources:
            sources.append(native["source"])

    return {
        "schema": LOCK_SCHEMA,
        "sources": sources,
        "packages": {k: packages[k] for k in sorted(packages)},
        "targets": {k: targets[k] for k in sorted(targets)},
        "unresolved": unresolved,
    }


_RSP_ARGUMENT = re.compile(r'"([^"]*)"|(\S+)')


def native_packages(report: dict) -> Dict[str, dict]:
    """What the C++ targets of `report` take from NuGet packages, per package
    key: {id, version, integrity, source, cc: {include_dirs, libs}, files:
    {target: [path in package]}}.

    A C++ project has no assets file. What it uses of a package is what its
    command lines name: include directories and libraries (`externals` in the
    report), the tools its build steps run, and the metadata files (`.winmd`)
    it hands to them through response files."""
    native = {name for name, t in report.get("targets", {}).items() if "csharp" not in t}
    out: Dict[str, dict] = {}
    roots: List[Tuple[str, str]] = []
    for external in report.get("externals", []):
        if external.get("kind") != "nuget" or not external.get("version"):
            continue
        consumers = sorted(native & set(external.get("consumers", [])))
        if not consumers:
            continue
        key = _key(external["id"], external["version"])
        roots.append((external["root"].replace("/", "\\").rstrip("\\").lower() + "\\", key))
        record = out.setdefault(key, {
            "id": external["id"], "version": external["version"],
            "integrity": external.get("integrity", ""), "source": external.get("source", ""),
            "cc": {}, "files": {}, "consumers": []})
        record["consumers"] = sorted(set(record["consumers"]) | set(consumers))
        for field in ("include_dirs", "libs"):
            if external.get(field):
                record["cc"][field] = sorted(set(record["cc"].get(field, [])) | set(external[field]))
        # A tool is recorded as the command spelled it, often without `.exe`.
        tools = [t if "." in t.rsplit("/", 1)[-1] else t + ".exe" for t in external.get("tools", [])]
        for consumer in consumers:
            record["files"].setdefault(consumer, []).extend(tools)
    def note(name: str, path: str) -> None:
        value = path.replace("/", "\\")
        low = value.lower()
        for root, key in roots:
            if low.startswith(root) and not low.endswith("\\."):
                out[key]["files"].setdefault(name, []).append(value[len(root):].replace("\\", "/"))
                return

    for name in sorted(native):
        target = report["targets"][name]
        # Sources a package has its consumers compile.
        for variant in target.get("compile", {}).get("variants", []):
            for source in variant.get("sources", []):
                note(name, source)
        for step in target.get("steps", []):
            if step.get("kind") != "write":
                continue
            for line in step.get("lines", []):
                for m in _RSP_ARGUMENT.finditer(line):
                    note(name, m.group(1) if m.group(1) is not None else m.group(2))
    for record in out.values():
        record["files"] = {t: sorted(set(f)) for t, f in sorted(record["files"].items()) if f}
    # A package that only contributed build logic has nothing to download.
    return {key: record for key, record in out.items() if record["cc"] or record["files"]}


def downloader(feeds: Sequence[str], cache_dir: str):
    """`fetch` for build_lock(): looks a package up on each feed in turn and
    keeps the .nupkg in `cache_dir`."""
    bases: Dict[str, Optional[str]] = {}

    def base_address(feed: str) -> Optional[str]:
        if feed not in bases:
            bases[feed] = None
            try:
                with urllib.request.urlopen(feed, timeout=60) as response:
                    index = json.load(response)
                for resource in index.get("resources", []):
                    if resource.get("@type", "").startswith("PackageBaseAddress"):
                        bases[feed] = resource["@id"].rstrip("/")
                        break
            except (OSError, ValueError):
                pass
        return bases[feed]

    def fetch(package_id: str, version: str):
        name = "%s.%s.nupkg" % (package_id.lower(), version.lower())
        path = os.path.join(cache_dir, name)
        marker = path + ".feed"
        if os.path.isfile(path) and os.path.isfile(marker):
            with open(marker, encoding="utf-8") as f:
                return path, f.read().strip()
        for feed in feeds:
            base = base_address(feed)
            if not base:
                continue
            url = "%s/%s/%s/%s" % (base, package_id.lower(), version.lower(), name)
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with urllib.request.urlopen(url, timeout=600) as response, \
                        open(path + ".part", "wb") as out:
                    for chunk in iter(lambda: response.read(1 << 20), b""):
                        out.write(chunk)
                os.replace(path + ".part", path)
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(feed + "\n")
                return path, feed
            except OSError:
                continue
        return None

    return fetch


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", help="msbuild.report.json written by extract_msbuild.py")
    parser.add_argument("repo_root", help="the tree the reference build ran in")
    parser.add_argument("out", help="nuget_lock.json to write")
    parser.add_argument("--toolchain-packs", default=",".join(TOOLCHAIN_PACKS),
                        help="framework packs the Bazel rule set supplies itself, as "
                             "comma-separated id prefixes (default: %(default)s); every "
                             "other pack the build read is locked as its NuGet package")
    parser.add_argument("--fetch-packs", action="store_true",
                        help="download the package of a framework pack that is in no NuGet "
                             "package folder, to hash it and compare its files with the "
                             "pack's (from the build's feeds, then nuget.org)")
    parser.add_argument("--pack-cache", help="where --fetch-packs keeps packages "
                                             "(default: <out>.packs beside the lock)")
    args = parser.parse_args(argv)
    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    fetch = None
    if args.fetch_packs:
        # The build's own feeds are not known before the lock is built; a first
        # pass without downloading finds them.
        feeds = build_lock(report, args.repo_root, verify=False)["sources"]
        fetch = downloader(feeds + [f for f in [NUGET_ORG] if f not in feeds],
                           args.pack_cache or args.out + ".packs")
    lock = build_lock(report, args.repo_root, fetch=fetch,
                      toolchain_packs=[p.strip().lower() for p in args.toolchain_packs.split(",")
                                       if p.strip()])
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(lock, f, indent=1, sort_keys=True)
        f.write("\n")
    print("wrote %s: %d package(s) for %d C# target(s), feeds: %s"
          % (args.out, len(lock["packages"]), len(lock["targets"]),
             ", ".join(lock["sources"]) or "none recorded"))
    for key, record in sorted(lock["packages"].items()):
        pack = record.get("pack")
        if not pack:
            continue
        if "identical" not in pack:
            print("  pack %s -> %s: not compared" % (pack["of"], key))
        else:
            print("  pack %s -> %s: %d file(s) identical, %d different, %d not compared"
                  % (pack["of"], key, pack["identical"], len(pack["different"]),
                     len(pack["unverified"])))
            for name in pack["different"][:10]:
                print("    DIFFERENT: %s" % name)
    if lock["unresolved"]:
        print("ERROR: no hash found for %d package(s); they cannot be fetched by integrity: %s"
              % (len(lock["unresolved"]), ", ".join(lock["unresolved"])), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
