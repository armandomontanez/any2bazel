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

"""Where does a path in an MSBuild build come from?

An MSBuild command line is full of absolute paths, and each one is a dependency
on something: the compiler install, the Windows SDK, a restored NuGet package,
a vcpkg tree, a file another build step generated, or the repository itself.
A Bazel build has to give each of those its own boundary, so the first job is
to say, for every path, which one it is.

Classification uses the BUILD'S OWN evaluated properties as roots
(`VCToolsInstallDir`, `WindowsSdkDir`, `NuGetPackageRoot`, `VcpkgInstalledDir`,
...) rather than guessing from path shapes, with two structural exceptions that
have no property: a `packages\\<Id>.<Version>\\` directory (packages.config
restore) and a package directory under the NuGet global folder.

Every root gets a stable TOKEN (`@msvc`, `@winsdk`, `@nuget/<id>`,
`@vcpkg/<triplet>`). The model stores the root -> token map so the differ
compares `@nuget/microsoft.windows.implementationlibrary/include` instead of a
machine path; the Bazel side maps its `external/<repo>` spelling onto the same
token with `ignore.include_map`.

All path logic uses `ntpath`, so results do not depend on the host OS. Reading
package hashes and vcpkg's database touches the filesystem and degrades to
"unknown" when the tree is not there.
"""

from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

REPO = "repo"
GENERATED = "generated"
MSVC = "msvc"
WINSDK = "winsdk"
VS = "vs"
DOTNET = "dotnet"
NUGET = "nuget"
VCPKG = "vcpkg"
SYSTEM = "system"
UNKNOWN = "unknown"

# packages.config restores into <somewhere>\packages\<Id>.<Version>\
_PACKAGES_DIR = re.compile(
    r"^(?P<id>.+?)\.(?P<version>\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.\-]+)?)$")


def win_norm(path: str) -> str:
    """Normalize a Windows path: backslashes, `..` folded, no trailing slash
    (drive roots excepted)."""
    p = ntpath.normpath(path.strip().strip('"').replace("/", "\\"))
    return p if len(p) <= 3 else p.rstrip("\\")


def is_abs(path: str) -> bool:
    return ntpath.isabs(path.replace("/", "\\")) and bool(ntpath.splitdrive(path)[0])


def _under(path_low: str, root_low: str) -> bool:
    return path_low == root_low or path_low.startswith(root_low + "\\")


def _posix(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


@dataclass
class Root:
    """One external (or build-output) root directory."""
    path: str                 # as recorded, normalized
    kind: str
    token: str
    id: str = ""
    version: str = ""
    # What the build was seen using from this root, as paths relative to it.
    include_dirs: Set[str] = field(default_factory=set)
    libs: Set[str] = field(default_factory=set)
    tools: Set[str] = field(default_factory=set)
    imports: Set[str] = field(default_factory=set)
    references: Set[str] = field(default_factory=set)
    consumers: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Classified:
    kind: str
    path: str                 # tokenized ("@nuget/x/include") or repo-relative
    root: Optional[Root] = None
    rel: str = ""             # path below the root, POSIX


class Origins:
    def __init__(self, repo_root: str):
        self.repo_root = win_norm(repo_root)
        self._repo_low = self.repo_root.lower()
        self._roots: Dict[str, Root] = {}           # lower-cased path -> Root
        self._nuget_global: List[str] = []          # lower-cased global folders
        self._build_dirs: Set[str] = set()          # lower-cased IntDir/OutDir
        self._tracked: Optional[Set[str]] = None    # lower-cased repo-relative
        self._tracked_loaded = False

    # ---- roots --------------------------------------------------------------

    def add_root(self, path: str, kind: str, token: str, id: str = "",
                 version: str = "") -> Optional[Root]:
        if not path or not is_abs(path):
            return None
        norm = win_norm(path)
        low = norm.lower()
        existing = self._roots.get(low)
        if existing is None:
            existing = self._roots[low] = Root(path=norm, kind=kind, token=token,
                                               id=id, version=version)
        elif version and not existing.version:
            existing.version = version
        return existing

    def learn(self, props) -> None:
        """Register the roots one evaluation's properties name. `props` is
        anything with `.get(name, default)` (case-insensitive lookups)."""
        g = lambda n: props.get(n, "")
        self.add_root(g("VCToolsInstallDir"), MSVC, "@msvc", "msvc", g("VCToolsVersion"))
        self.add_root(g("VCInstallDir"), MSVC, "@vc", "vc")
        sdk_version = g("WindowsTargetPlatformVersion") or g("TargetPlatformVersion")
        self.add_root(g("WindowsSdkDir"), WINSDK, "@winsdk", "windows-sdk", sdk_version)
        self.add_root(g("UniversalCRTSdkDir"), WINSDK, "@winsdk", "windows-sdk", sdk_version)
        self.add_root(g("NETFXKitsDir"), WINSDK, "@netfxsdk", "netfx-sdk")
        self.add_root(g("VSInstallDir") or g("VsInstallRoot"), VS, "@vs", "visual-studio",
                      g("VisualStudioVersion"))
        packs = g("NetCoreTargetingPackRoot")
        if packs and is_abs(packs):
            self.add_root(ntpath.dirname(win_norm(packs)), DOTNET, "@dotnet", "dotnet",
                          g("NETCoreSdkVersion"))
        for folder in (g("NuGetPackageRoot") + ";" + g("NuGetPackageFolders")).split(";"):
            if folder.strip() and is_abs(folder.strip()):
                low = win_norm(folder).lower()
                if low not in self._nuget_global:
                    self._nuget_global.append(low)
        installed = g("VcpkgInstalledDir")
        triplet = g("VcpkgTriplet")
        current = g("_ZVcpkgCurrentInstalledDir") or g("VcpkgCurrentInstalledDir")
        if not current and installed and triplet and is_abs(installed):
            current = ntpath.join(win_norm(installed), triplet)
        if current and is_abs(current):
            name = triplet or ntpath.basename(win_norm(current))
            self.add_root(current, VCPKG, "@vcpkg/" + name, "vcpkg", name)
        if installed and is_abs(installed):
            self.add_root(installed, VCPKG, "@vcpkg", "vcpkg")
        self.add_root(g("VcpkgRoot"), VCPKG, "@vcpkg-tool", "vcpkg-tool")
        project_dir = g("ProjectDir") or ntpath.dirname(g("MSBuildProjectFullPath"))
        for name in ("IntDir", "OutDir", "IntermediateOutputPath", "OutputPath",
                     "BaseIntermediateOutputPath", "GeneratedFilesDir"):
            value = g(name)
            if not value:
                continue
            if not is_abs(value) and project_dir:
                value = ntpath.join(project_dir, value)
            if is_abs(value):
                self._build_dirs.add(win_norm(value).lower())

    def _nuget_root(self, norm: str) -> Optional[Root]:
        """Recognize (and register) a NuGet package directory in `norm`."""
        low = norm.lower()
        for folder in self._nuget_global:
            if _under(low, folder) and low != folder:
                parts = norm[len(folder):].strip("\\").split("\\")
                if len(parts) >= 2:
                    root = ntpath.join(norm[:len(folder)], parts[0], parts[1])
                    return self.add_root(root, NUGET, "@nuget/" + parts[0].lower(),
                                         parts[0].lower(), parts[1])
        parts = norm.split("\\")
        for i in range(1, len(parts)):
            if parts[i - 1].lower() != "packages":
                continue
            m = _PACKAGES_DIR.match(parts[i])
            if m:
                root = "\\".join(parts[:i + 1])
                return self.add_root(root, NUGET, "@nuget/" + m.group("id").lower(),
                                     m.group("id"), m.group("version"))
        return None

    # ---- classification -----------------------------------------------------

    def classify(self, path: str) -> Classified:
        """Classify an ABSOLUTE path. Relative paths must be resolved against
        the action's working directory by the caller first."""
        if not is_abs(path):
            return Classified(UNKNOWN, path.replace("\\", "/"))
        norm = win_norm(path)
        low = norm.lower()
        root = self._nuget_root(norm)
        if root is None:
            best = None
            for root_low, candidate in self._roots.items():
                # A root that CONTAINS the repository (a property pointing at a
                # parent directory) says nothing about a path inside the repo.
                if _under(self._repo_low, root_low):
                    continue
                if _under(low, root_low) and (best is None or len(root_low) > len(best[0])):
                    best = (root_low, candidate)
            root = best[1] if best else None
        if root is not None:
            rel = _posix(norm[len(root.path):])
            return Classified(root.kind, root.token + ("/" + rel if rel else ""), root, rel)
        if _under(low, self._repo_low):
            rel = _posix(norm[len(self.repo_root):])
            kind = GENERATED if self.is_generated(norm) else REPO
            return Classified(kind, rel or ".", None, rel)
        parts = low.split("\\")
        if len(parts) > 1 and parts[1] == "windows":
            return Classified(SYSTEM, norm.replace("\\", "/"))
        if "\\reference assemblies\\microsoft\\framework" in low:
            return Classified(DOTNET, norm.replace("\\", "/"))
        return Classified(UNKNOWN, norm.replace("\\", "/"))

    def note(self, path: str, role: str, consumer: str = "") -> Classified:
        """Classify `path` and record that the build used it. `role` is one of
        'include', 'lib', 'tool', 'import', 'reference'."""
        c = self.classify(path)
        if c.root is not None:
            bucket = {"include": c.root.include_dirs, "lib": c.root.libs,
                      "tool": c.root.tools, "import": c.root.imports,
                      "reference": c.root.references}.get(role)
            if bucket is not None:
                bucket.add(c.rel)
            if consumer:
                c.root.consumers.add(consumer)
        return c

    def roots(self) -> List[Root]:
        return sorted(self._roots.values(), key=lambda r: (r.kind, r.token, r.path.lower()))

    def path_map(self) -> List[Tuple[str, str]]:
        """(root path, token) pairs for the model, longest path first. A NuGet
        package is included only if a compile or link actually used a path
        under it; one that merely imported build logic has nothing to map."""
        pairs = [(r.path, r.token) for r in self._roots.values()
                 if not _under(self._repo_low, r.path.lower())
                 and (r.kind != NUGET or r.include_dirs or r.libs or r.tools or r.references)]
        return sorted(pairs, key=lambda p: (-len(p[0]), p[0].lower()))

    # ---- generated files ----------------------------------------------------

    def is_build_dir(self, norm: str) -> bool:
        low = norm.lower()
        return any(_under(low, d) for d in self._build_dirs)

    def set_tracked(self, repo_relative_paths: Optional[Iterable[str]]) -> None:
        """Provide the set of git-tracked files (repo-relative). None means
        unknown, in which case only build directories count as generated."""
        self._tracked_loaded = True
        self._tracked = (None if repo_relative_paths is None else
                         {p.replace("\\", "/").lower() for p in repo_relative_paths})

    def load_tracked_from_git(self) -> bool:
        """Ask git which files are tracked. Returns False (and leaves the set
        unknown) when the repo root is not a git checkout on this machine."""
        self._tracked_loaded = True
        try:
            out = subprocess.run(["git", "-C", self.repo_root, "ls-files", "-z"],
                                 capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self._tracked = None
            return False
        self._tracked = {p.lower() for p in out.decode("utf-8", "replace").split("\0") if p}
        return True

    def is_generated(self, norm: str) -> bool:
        """A file under the repo that the build produced: inside a project's
        intermediate/output directory, or not tracked by git."""
        if self.is_build_dir(norm):
            return True
        if self._tracked is None:
            return False
        rel = _posix(norm[len(self.repo_root):]).lower()
        return bool(rel) and rel not in self._tracked

    # ---- package facts (filesystem; all best effort) -------------------------

    def nuget_facts(self, root: Root) -> Dict[str, str]:
        """Content hash and source feed of a restored NuGet package, read from
        what NuGet left on disk. Missing facts are simply absent -- never
        guessed."""
        facts: Dict[str, str] = {}
        meta = os.path.join(root.path, ".nupkg.metadata")
        try:
            with open(meta, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("contentHash"):
                facts["sha512"] = data["contentHash"]
            if data.get("source"):
                facts["source"] = data["source"]
        except (OSError, ValueError):
            pass
        if "sha512" not in facts:
            try:
                names = os.listdir(root.path)
            except OSError:
                names = []
            for name in names:
                low = name.lower()
                if low.endswith(".nupkg.sha512"):
                    try:
                        with open(os.path.join(root.path, name), encoding="utf-8") as f:
                            facts["sha512"] = f.read().strip()
                    except OSError:
                        pass
                    break
            else:
                for name in names:
                    if name.lower().endswith(".nupkg"):
                        h = hashlib.sha512()
                        try:
                            with open(os.path.join(root.path, name), "rb") as f:
                                for chunk in iter(lambda: f.read(1 << 20), b""):
                                    h.update(chunk)
                            facts["sha512"] = base64.b64encode(h.digest()).decode()
                        except OSError:
                            pass
                        break
        # Bazel's `integrity` attribute takes exactly this form (SRI), so the
        # hash NuGet recorded can be used as is.
        if "sha512" in facts:
            facts["integrity"] = "sha512-" + facts.pop("sha512")
        return facts

    def vcpkg_ports(self, installed_dir: str) -> List[Dict[str, object]]:
        """Installed ports from vcpkg's own database (`vcpkg\\status` and
        `vcpkg\\info\\*.list`): name, version, triplet, features, files."""
        status = os.path.join(installed_dir, "vcpkg", "status")
        try:
            with open(status, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return []
        ports: Dict[Tuple[str, str], Dict[str, object]] = {}
        for para in re.split(r"\r?\n\r?\n", text):
            fields: Dict[str, str] = {}
            for line in para.splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    fields[k.strip()] = v.strip()
            name, triplet = fields.get("Package"), fields.get("Architecture")
            if not name or not triplet or "install ok installed" not in fields.get("Status", ""):
                continue
            port = ports.setdefault((name, triplet), {
                "name": name, "triplet": triplet, "version": "", "features": []})
            if "Feature" in fields:
                port["features"].append(fields["Feature"])  # type: ignore[union-attr]
            else:
                version = fields.get("Version", "")
                if fields.get("Port-Version") and fields["Port-Version"] != "0":
                    version += "#" + fields["Port-Version"]
                port["version"] = version
        info = os.path.join(installed_dir, "vcpkg", "info")
        try:
            lists = os.listdir(info)
        except OSError:
            lists = []
        for name in lists:
            if not name.endswith(".list"):
                continue
            stem = name[:-5]
            for (pname, triplet), port in ports.items():
                if stem.startswith(pname + "_") and stem.endswith("_" + triplet):
                    try:
                        with open(os.path.join(info, name), encoding="utf-8",
                                  errors="replace") as f:
                            files = [l.strip() for l in f if l.strip() and not l.strip().endswith("/")]
                    except OSError:
                        files = []
                    port["libs"] = sorted(ntpath.basename(x.replace("/", "\\")) for x in files
                                          if x.lower().endswith(".lib")
                                          and "/debug/" not in x.lower())
                    break
        return sorted(ports.values(), key=lambda p: (p["triplet"], p["name"]))  # type: ignore[index]
