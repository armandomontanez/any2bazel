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

"""MSBuild build events: replay a binary log to NDJSON, then correlate it.

MSBuild has no action graph to query, but a binary log (`-bl`) records what a
real build did: every project evaluation (properties and items), every target
and task that ran or was skipped, every tool command line, and every task
parameter. This module turns that record into plain Python objects.

Two halves, deliberately separated:

  * `dump_binlog()` -- Windows only. Compiles scripts/msbuild/Any2BazelLogger.cs
    with the csc.exe that ships inside MSBuild and asks MSBuild to REPLAY the
    binlog through it (`msbuild x.binlog -logger:...`). MSBuild reads its own
    format, so there is no reader library to keep in step with it.
  * `load()` -- pure Python, any OS. Reads the NDJSON the logger wrote and
    correlates the flat event stream into Project -> Target -> Task records.
    This is what the extractor and the tests use.

The NDJSON is the contract between the two. One JSON object per line; `e` names
the record. Context keys: `p` project context id, `t` target id, `k` task id,
`ev` evaluation id, `n` node id (each omitted when not applicable).

  Dump            schema
  LongValue       id, value   -- a property value over ~2 KB is written once
                  here and referenced as the string "@long:<id>" afterwards
  BuildStarted / BuildFinished(succeeded)
  Eval            file, global{}, props{}, items[{t: type, i: spec, m: {meta}}]
  Import          file (importer), imported
  EnvRead         name
  ProjectStarted  file, targets, parent, parentTarget, parentTask, global{}
  ProjectFinished file, succeeded
  TargetStarted   name, file, parent
  TargetFinished  name, succeeded
  TargetSkipped   name, reason (ConditionWasFalse | OutputsUpToDate |
                  PreviouslyBuiltSuccessfully | ...), condition, evaluated
  TaskStarted     name, file, line
  TaskFinished    name, succeeded
  CommandLine     task, cmd
  TaskParameter   kind (TaskInput | TaskOutput | AddItem | RemoveItem |
                  SkippedTargetInputs | SkippedTargetOutputs), name, itemType,
                  items[{i: spec, m: {meta}}]   -- scalar parameters arrive as
                  one item whose spec is the value
  Error           code, file, line, project, msg
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

_LOGGER_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "msbuild", "Any2BazelLogger.cs")

# Evaluated properties the extractor reads. A trailing '*' is a prefix match.
# This is an allowlist because a full property dump is ~1000 entries per
# evaluation and a large solution evaluates hundreds of times.
DEFAULT_PROPERTIES = (
    # identity and layout
    "MSBuildProjectFullPath", "MSBuildProjectName", "MSBuildProjectExtension",
    "ProjectName", "ProjectDir", "ProjectGuid", "ProjectTypeGuids",
    "Configuration", "Platform", "PlatformTarget", "SolutionDir", "SolutionPath",
    "OutDir", "OutputPath", "IntDir", "IntermediateOutputPath",
    "BaseIntermediateOutputPath", "TargetName", "TargetExt", "TargetPath",
    "TargetFileName", "TargetDir", "AssemblyName", "RootNamespace",
    # MSBuild / Visual Studio
    "MSBuildVersion", "MSBuildToolsPath", "MSBuildBinPath", "MSBuildExtensionsPath",
    "VisualStudioVersion", "VSInstallDir", "VsInstallRoot",
    # C++ project shape and toolchain
    "ConfigurationType", "Keyword", "PlatformToolset", "PlatformToolsetVersion",
    "VCToolsVersion", "VCToolsInstallDir", "VCInstallDir", "VCToolArchitecture",
    "PreferredToolArchitecture", "WindowsTargetPlatformVersion",
    "TargetPlatformVersion", "WindowsTargetPlatformMinVersion", "WindowsSdkDir",
    "UniversalCRTSdkDir", "NETFXKitsDir", "IncludePath", "ExternalIncludePath",
    "LibraryPath", "ReferencePath", "ExecutablePath", "CharacterSet", "UseOfMfc",
    "UseOfAtl", "CLRSupport", "WholeProgramOptimization", "SpectreMitigation",
    "UseDebugLibraries", "TLogLocation", "GeneratedFilesDir", "WindowsAppContainer",
    "ApplicationType", "LinkIncremental", "GenerateManifest", "EmbedManifest",
    "EnableASAN", "EnableFuzzer",
    "Vcpkg*", "_ZVcpkg*", "CppWinRT*",
    # NuGet
    "NuGetPackageRoot", "NuGetPackageFolders", "RestorePackagesPath",
    "RestoreProjectStyle", "NuGetProjectStyle", "ProjectAssetsFile",
    "RestoreSources", "ManagePackageVersionsCentrally",
    # .NET
    "TargetFramework", "TargetFrameworks", "TargetFrameworkVersion",
    "TargetFrameworkMoniker", "TargetFrameworkIdentifier",
    "TargetPlatformIdentifier", "RuntimeIdentifier", "RuntimeIdentifiers",
    "SelfContained", "OutputType", "UseWPF", "UseWinUI", "UseWindowsForms",
    "Nullable", "LangVersion", "AllowUnsafeBlocks", "DefineConstants",
    "IsTestProject", "IsPackable", "PublishAot", "NETCoreSdkVersion",
    "UsingMicrosoftNETSdk", "MSBuildSDKsPath", "NetCoreTargetingPackRoot",
    "EnableMSTestRunner", "CsWinRT*",
    # tests
    "RunVSTest", "VSTestConsolePath", "TestingPlatformDotnetTestSupport",
)

# Evaluated item types the extractor reads.
DEFAULT_ITEM_TYPES = (
    "ClCompile", "ClInclude", "Midl", "ResourceCompile", "CustomBuild", "MASM",
    "FxCompile", "Manifest", "Natvis", "CopyFileToFolders", "PRIResource",
    "AppxManifest", "Page", "ApplicationDefinition",
    "ProjectReference", "Reference", "PackageReference", "PackageVersion",
    "COMReference", "SDKReference", "FrameworkReference", "NativeReference",
    "Compile", "EmbeddedResource", "Content", "None",
    "ProjectCapability", "ProjectConfiguration",
    # Not evaluated: the list a target computes for MSBuild's copy-if-newer
    # target, whose Copy task is only handed the part that is out of date.
    "_SourceItemsToCopyToOutputDirectory",
)

# Bookkeeping tasks whose parameters are not dumped. They carry nothing the
# extractor reads and account for most of a dump's bulk (reference resolution
# alone logs every candidate assembly). This is a DENY list on purpose: the
# parameters of a task nobody anticipated are kept, and every task's
# start/finish is always recorded, so an unknown task is never invisible.
QUIET_TASKS = (
    "ResolveAssemblyReference", "ResolvePackageAssets", "ResolvePackageFileConflicts",
    "ResolveTargetingPackAssets", "ResolveRuntimePackAssets", "ResolveFrameworkReferences",
    "ResolveAppHosts", "ProcessFrameworkReferences", "CheckForUnsupportedWinMDReferences",
    "CleanupStaleTlogItems", "ConvertToAbsolutePath", "AssignTargetPath", "FindUnderPath",
    "RemoveDuplicates", "Hash", "JoinItems", "GetPackageDirectory", "AssignCulture",
    "CheckForDuplicateItems", "AssignProjectConfiguration", "Message",
    "GetReferenceNearestTargetFrameworkTask", "ResolveCopyLocalNuGetPackageAssets",
    "GenerateDepsFile", "CheckForImplicitPackageReferenceOverrides",
    "ApplyImplicitVersions", "CreateWindowsSdkKnownFrameworkReferences",
)


# ---- replay: binlog -> NDJSON (Windows only) -------------------------------

class MSBuildNotFound(RuntimeError):
    pass


def find_msbuild(explicit: Optional[str] = None) -> str:
    """Locate MSBuild.exe: explicit path, $MSBUILD_EXE, the newest Visual Studio
    (via vswhere), then PATH. The newest install is preferred because a newer
    MSBuild reads older binlogs, not the reverse."""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("MSBUILD_EXE"):
        candidates.append(os.environ["MSBUILD_EXE"])
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        vswhere = os.path.join(base, "Microsoft Visual Studio", "Installer", "vswhere.exe")
        if os.path.isfile(vswhere):
            try:
                out = subprocess.run(
                    [vswhere, "-latest", "-products", "*", "-requires",
                     "Microsoft.Component.MSBuild", "-find", r"MSBuild\**\Bin\MSBuild.exe"],
                    capture_output=True, text=True, check=False).stdout
            except OSError:
                out = ""
            candidates.extend(line.strip() for line in out.splitlines() if line.strip())
            break
    on_path = shutil.which("msbuild")
    if on_path:
        candidates.append(on_path)
    for c in candidates:
        if os.path.isfile(c):
            # Prefer the 64-bit host: replaying a large binlog is memory hungry.
            amd64 = os.path.join(os.path.dirname(c), "amd64", os.path.basename(c))
            return amd64 if os.path.isfile(amd64) else c
    raise MSBuildNotFound(
        "MSBuild.exe not found; pass --msbuild <path> or set MSBUILD_EXE "
        "(looked at: %s)" % (", ".join(candidates) or "vswhere, PATH"))


def _bin_root(msbuild_exe: str) -> str:
    """The MSBuild `Bin` directory holding Roslyn\\ and ref\\ (MSBuild.exe may
    live in an architecture subdirectory of it)."""
    d = os.path.dirname(os.path.abspath(msbuild_exe))
    if not os.path.isdir(os.path.join(d, "Roslyn")):
        parent = os.path.dirname(d)
        if os.path.isdir(os.path.join(parent, "Roslyn")):
            return parent
    return d


def _cache_dir() -> str:
    root = (os.environ.get("ANY2BAZEL_CACHE")
            or os.path.join(os.environ.get("LOCALAPPDATA")
                            or os.path.join(os.path.expanduser("~"), ".cache"),
                            "any2bazel"))
    return os.path.join(root, "msbuild-logger")


def build_logger(msbuild_exe: str) -> str:
    """Compile the logger with MSBuild's own csc.exe and return the DLL path.

    Same recipe MSBuild's RoslynCodeTaskFactory uses for inline tasks: compile
    against the netstandard reference assemblies under MSBuild's `ref`
    directory, so the result loads in the MSBuild that will replay the binlog.
    Cached per (logger source, MSBuild install); the cache lives outside the
    skill directory, which may be read-only.
    """
    root = _bin_root(msbuild_exe)
    csc = os.path.join(root, "Roslyn", "csc.exe")
    framework = os.path.join(root, "Microsoft.Build.Framework.dll")
    refs = [os.path.join(root, "ref", "mscorlib.dll"),
            os.path.join(root, "ref", "netstandard.dll"), framework]
    missing = [p for p in [csc] + refs if not os.path.isfile(p)]
    if missing:
        raise MSBuildNotFound(
            "cannot compile the event logger: %s not found next to %s. A Visual "
            "Studio (or Build Tools) MSBuild is required." % (missing[0], msbuild_exe))

    with open(_LOGGER_SOURCE, "rb") as f:
        source = f.read()
    stat = os.stat(framework)
    key = hashlib.sha256(source + ("%s|%d|%d" % (framework, stat.st_size,
                                                 int(stat.st_mtime))).encode()).hexdigest()[:16]
    out_dir = os.path.join(_cache_dir(), key)
    dll = os.path.join(out_dir, "Any2BazelLogger.dll")
    if os.path.isfile(dll):
        return dll
    os.makedirs(out_dir, exist_ok=True)
    cmd = [csc, "-nologo", "-noconfig", "-nostdlib+", "-target:library",
           "-optimize+", "-deterministic+", "-nowarn:1701,1702"]
    cmd += ["-reference:" + r for r in refs]
    cmd += ["-out:" + dll, _LOGGER_SOURCE]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0 or not os.path.isfile(dll):
        raise RuntimeError("compiling %s failed:\n%s%s"
                           % (_LOGGER_SOURCE, res.stdout, res.stderr))
    return dll


def _write_filter(path: str, properties: Iterable[str], item_types: Iterable[str],
                  quiet_tasks: Iterable[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for p in properties:
            f.write("P %s\n" % p)
        for i in item_types:
            f.write("I %s\n" % i)
        for t in quiet_tasks:
            f.write("X %s\n" % t)


def dump_binlog(binlog: str, out_ndjson: str, msbuild: Optional[str] = None,
                properties: Iterable[str] = DEFAULT_PROPERTIES,
                item_types: Iterable[str] = DEFAULT_ITEM_TYPES,
                quiet_tasks: Iterable[str] = QUIET_TASKS) -> str:
    """Replay `binlog` through the logger, writing `out_ndjson`. Returns the
    MSBuild.exe used.

    The replay's exit code is ignored on purpose: it mirrors the RECORDED
    build's result, and a failed build is still worth extracting. Success is
    judged by the dump itself (it must end with a BuildFinished record).
    """
    exe = find_msbuild(msbuild)
    dll = build_logger(exe)
    binlog = os.path.abspath(binlog)
    out_ndjson = os.path.abspath(out_ndjson)
    if not os.path.isfile(binlog):
        raise FileNotFoundError(binlog)
    if os.path.exists(out_ndjson):
        os.remove(out_ndjson)
    fd, filter_path = tempfile.mkstemp(prefix="any2bazel-filter-", suffix=".txt")
    os.close(fd)
    try:
        _write_filter(filter_path, properties, item_types, quiet_tasks)
        logger = "-logger:Any2BazelLogger,%s;out=%s;filter=%s" % (dll, out_ndjson, filter_path)
        res = subprocess.run([exe, binlog, "-nologo", "-noconlog", logger],
                             capture_output=True, text=True, check=False)
    finally:
        try:
            os.remove(filter_path)
        except OSError:
            pass
    if not os.path.isfile(out_ndjson) or not _dump_is_complete(out_ndjson):
        output = res.stdout[-4000:] + res.stderr[-4000:]
        hint = ""
        if "log file format version" in output:
            hint = ("\nThis MSBuild is older than the one that wrote the binlog. Replay "
                    "with the MSBuild that built it, or a newer one (--msbuild).")
        raise RuntimeError(
            "replaying %s with %s produced no complete event dump (exit %d):\n%s%s"
            % (binlog, exe, res.returncode, output, hint))
    return exe


# What makes MSBuild compute a C# compile without running it. This is the
# property set an IDE's design-time build uses: CoreCompile always runs (it is
# never "up to date"), the Csc task returns its command line as items instead
# of starting the compiler, and referenced projects are asked where their
# output WILL be instead of being built.
DESIGN_TIME_PROPERTIES = {
    "DesignTimeBuild": "true",
    "BuildingInsideVisualStudio": "true",
    "BuildProjectReferences": "false",
    "SkipCompilerExecution": "true",
    "ProvideCommandLineArgs": "true",
}


# `Compile` alone leaves out what a build does before it. Resource generation
# is the part that reaches the compiler: without it every `/resource:` of a
# `.resx` is missing from the computed command line.
DESIGN_TIME_TARGETS = "PrepareResources;Compile"


def _msbuild_escape(value: str) -> str:
    """Escape a property value for the MSBuild task's `Properties` attribute,
    where `;` separates properties and `$( @( %(` would be expanded."""
    out = value.replace("%", "%25")
    for ch in ";$@'*?":
        out = out.replace(ch, "%%%02X" % ord(ch))
    return out


def design_time_project(instances: Sequence[Tuple[str, Dict[str, str]]]) -> str:
    """Text of a traversal project that runs DESIGN_TIME_TARGETS of each
    (project file, global properties) instance under DESIGN_TIME_PROPERTIES."""
    from xml.sax.saxutils import quoteattr
    lines = ["<Project>", '  <Target Name="Any2BazelDesignTime">']
    for project_file, global_properties in instances:
        merged = dict(global_properties)
        merged.update(DESIGN_TIME_PROPERTIES)
        properties = ";".join("%s=%s" % (k, _msbuild_escape(v)) for k, v in sorted(merged.items())
                              if k and v is not None)
        lines.append("    <MSBuild Projects=%s Targets=\"%s\" Properties=%s "
                     "ContinueOnError=\"true\" />"
                     % (quoteattr(project_file), DESIGN_TIME_TARGETS, quoteattr(properties)))
    lines += ["  </Target>", "</Project>", ""]
    return "\n".join(lines)


def design_time_compile(instances: Sequence[Tuple[str, Dict[str, str]]], out_ndjson: str,
                        msbuild: Optional[str] = None) -> str:
    """Ask MSBuild what it WOULD pass to the C# compiler for each instance,
    without compiling anything, and dump that build's events to `out_ndjson`.
    Returns the MSBuild.exe used.

    A compile MSBuild skips as up to date logs no command line, and C# has no
    tracking log to fall back on. The instances are run with exactly the
    global properties the real build gave them (the caller reads those from
    the real build's log), so the computed command lines are the ones a
    recompile would use; the extractor checks that claim against every
    project that did compile.
    """
    exe = find_msbuild(msbuild)
    work = tempfile.mkdtemp(prefix="any2bazel-designtime-")
    project = os.path.join(work, "designtime.proj")
    binlog = os.path.join(work, "designtime.binlog")
    try:
        with open(project, "w", encoding="utf-8", newline="\n") as f:
            f.write(design_time_project(instances))
        # Through a binary log and a replay, like the real build: only the
        # binary logger makes MSBuild record task outputs, which is where the
        # computed command line is.
        res = subprocess.run([exe, project, "-nologo", "-noconlog", "-m", "-bl:" + binlog],
                             capture_output=True, text=True, check=False)
        if not os.path.isfile(binlog):
            raise RuntimeError("the design-time pass wrote no log (exit %d):\n%s"
                               % (res.returncode, res.stdout[-4000:] + res.stderr[-4000:]))
        dump_binlog(binlog, out_ndjson, exe)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return exe


def _dump_is_complete(path: str) -> bool:
    """True if the last record is BuildFinished (the replay reached the end)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8", "replace").strip().splitlines()
    if not tail:
        return False
    try:
        return json.loads(tail[-1]).get("e") == "BuildFinished"
    except ValueError:
        return False            # a truncated last line: the replay was cut short


# ---- correlation: NDJSON -> objects (pure, any OS) -------------------------

@dataclass
class Item:
    """An MSBuild item: its spec (evaluated include) and custom metadata."""
    spec: str
    meta: Dict[str, str] = field(default_factory=dict)

    def get(self, name: str, default: str = "") -> str:
        """Metadata lookup; MSBuild metadata names are case-insensitive."""
        if name in self.meta:
            return self.meta[name]
        low = name.lower()
        for k, v in self.meta.items():
            if k.lower() == low:
                return v
        return default


class _Props:
    """Case-insensitive property bag (MSBuild property names are)."""

    def __init__(self, raw: Optional[Dict[str, str]] = None):
        self._d = {k.lower(): v for k, v in (raw or {}).items()}

    def get(self, name: str, default: str = "") -> str:
        return self._d.get(name.lower(), default)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._d

    def __len__(self) -> int:
        return len(self._d)


@dataclass
class Evaluation:
    id: int
    file: str = ""
    props: _Props = field(default_factory=_Props)
    globals: Dict[str, str] = field(default_factory=dict)
    items: Dict[str, List[Item]] = field(default_factory=dict)
    imports: List[str] = field(default_factory=list)


@dataclass
class Task:
    id: int
    name: str
    file: str = ""
    line: int = 0
    command_lines: List[str] = field(default_factory=list)
    inputs: Dict[str, List[Item]] = field(default_factory=dict)
    outputs: Dict[str, List[Item]] = field(default_factory=dict)
    succeeded: Optional[bool] = None

    def scalar(self, name: str, default: str = "") -> str:
        """A scalar input parameter (logged as a one-item list)."""
        items = self.inputs.get(name)
        return items[0].spec if items else default


@dataclass
class Target:
    id: int
    name: str
    file: str = ""
    tasks: List[Task] = field(default_factory=list)
    # Items a target body added (`<ItemGroup>` inside a target), by item type.
    added: Dict[str, List[Item]] = field(default_factory=dict)
    # The same with removals, in the order they happened: (added?, item type, items).
    item_changes: List[Tuple[bool, str, List[Item]]] = field(default_factory=list)
    succeeded: Optional[bool] = None


@dataclass
class SkippedTarget:
    name: str
    reason: str = ""
    condition: str = ""
    # Populated for reason == OutputsUpToDate (the target's declared I/O).
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)


@dataclass
class Project:
    """One project INSTANCE: a project file built with one set of global
    properties, for one set of entry targets."""
    ctx: int
    file: str = ""
    eval_id: int = -1
    entry_targets: str = ""
    globals: Dict[str, str] = field(default_factory=dict)
    parent: int = -1
    parent_task: int = -1
    targets: List[Target] = field(default_factory=list)
    skipped: List[SkippedTarget] = field(default_factory=list)
    succeeded: Optional[bool] = None

    def tasks(self, *names: str) -> Iterator[Task]:
        wanted = {n.lower() for n in names}
        for target in self.targets:
            for task in target.tasks:
                if not wanted or task.name.lower() in wanted:
                    yield task

    def target(self, name: str) -> Optional[Target]:
        for t in self.targets:
            if t.name == name:
                return t
        return None


@dataclass
class BuildError:
    code: str = ""
    file: str = ""
    line: int = 0
    project: str = ""
    message: str = ""


@dataclass
class BuildLog:
    schema: int = 0
    succeeded: Optional[bool] = None
    evaluations: Dict[int, Evaluation] = field(default_factory=dict)
    projects: Dict[int, Project] = field(default_factory=dict)
    errors: List[BuildError] = field(default_factory=list)
    env_read: List[str] = field(default_factory=list)
    dumper_errors: List[str] = field(default_factory=list)

    def evaluation_of(self, project: Project) -> Optional[Evaluation]:
        return self.evaluations.get(project.eval_id)


def _items(raw) -> List[Item]:
    return [Item(spec=i.get("i", ""), meta=i.get("m") or {}) for i in (raw or [])]


def load(path: str) -> BuildLog:
    """Read an event dump and correlate it."""
    with open(path, encoding="utf-8") as f:
        return correlate(json.loads(line) for line in f if line.strip())


def correlate(records: Iterable[dict]) -> BuildLog:
    """Fold a flat event stream into Evaluation / Project / Target / Task
    records. Tolerant by design: an event whose owner was never announced is
    attached to a placeholder rather than dropped."""
    log = BuildLog()
    long_values: Dict[str, str] = {}           # "@long:<id>" -> full value
    targets: Dict[tuple, Target] = {}          # (project ctx, target id)
    tasks: Dict[tuple, Task] = {}              # (project ctx, target id, task id)
    skipped: Dict[tuple, SkippedTarget] = {}   # (project ctx, target id)

    def evaluation(eid: int) -> Evaluation:
        if eid not in log.evaluations:
            log.evaluations[eid] = Evaluation(id=eid)
        return log.evaluations[eid]

    def project(ctx: int) -> Project:
        if ctx not in log.projects:
            log.projects[ctx] = Project(ctx=ctx)
        return log.projects[ctx]

    def target(r: dict) -> Target:
        key = (r.get("p", -1), r.get("t", -1))
        if key not in targets:
            targets[key] = Target(id=key[1], name=r.get("name", ""))
            project(key[0]).targets.append(targets[key])
        return targets[key]

    def task(r: dict) -> Task:
        key = (r.get("p", -1), r.get("t", -1), r.get("k", -1))
        if key not in tasks:
            tasks[key] = Task(id=key[2], name=r.get("name") or r.get("task") or "")
            target({"p": key[0], "t": key[1]}).tasks.append(tasks[key])
        return tasks[key]

    def props(raw: Optional[dict]) -> Dict[str, str]:
        if not raw or not long_values:
            return raw or {}
        return {k: long_values.get(v, v) for k, v in raw.items()}

    for r in records:
        kind = r.get("e")
        if kind == "Dump":
            log.schema = r.get("schema", 0)
        elif kind == "LongValue":
            long_values["@long:%s" % r.get("id")] = r.get("value", "")
        elif kind == "BuildFinished":
            log.succeeded = bool(r.get("succeeded"))
        elif kind == "Eval":
            ev = evaluation(r.get("ev", -1))
            ev.file = r.get("file", "")
            ev.globals = props(r.get("global"))
            ev.props = _Props(props(r.get("props")))
            for it in r.get("items") or []:
                ev.items.setdefault(it.get("t", ""), []).append(
                    Item(spec=it.get("i", ""), meta=it.get("m") or {}))
        elif kind == "Import":
            evaluation(r.get("ev", -1)).imports.append(r.get("imported", ""))
        elif kind == "EnvRead":
            name = r.get("name", "")
            if name and name not in log.env_read:
                log.env_read.append(name)
        elif kind == "ProjectStarted":
            p = project(r.get("p", -1))
            p.file = r.get("file", "")
            p.eval_id = r.get("ev", -1)
            p.entry_targets = r.get("targets", "")
            p.globals = props(r.get("global"))
            p.parent = r.get("parent", -1)
            p.parent_task = r.get("parentTask", -1)
            # Older MSBuild attaches properties/items here, not to an Eval.
            if r.get("props") or r.get("items"):
                ev = evaluation(p.eval_id if p.eval_id >= 0 else -1000 - p.ctx)
                p.eval_id = ev.id
                ev.file = ev.file or p.file
                if r.get("props") and not len(ev.props):
                    ev.props = _Props(props(r["props"]))
                if r.get("items") and not ev.items:
                    for it in r["items"]:
                        ev.items.setdefault(it.get("t", ""), []).append(
                            Item(spec=it.get("i", ""), meta=it.get("m") or {}))
        elif kind == "ProjectFinished":
            project(r.get("p", -1)).succeeded = bool(r.get("succeeded"))
        elif kind == "TargetStarted":
            t = target(r)
            t.name = r.get("name", t.name)
            t.file = r.get("file", "")
        elif kind == "TargetFinished":
            target(r).succeeded = bool(r.get("succeeded"))
        elif kind == "TargetSkipped":
            s = SkippedTarget(name=r.get("name", ""), reason=r.get("reason", ""),
                              condition=r.get("condition", ""))
            project(r.get("p", -1)).skipped.append(s)
            if "t" in r:
                skipped[(r.get("p", -1), r["t"])] = s
        elif kind == "TaskStarted":
            t = task(r)
            t.name = r.get("name", t.name)
            t.file = r.get("file", "")
            t.line = r.get("line", 0)
        elif kind == "TaskFinished":
            task(r).succeeded = bool(r.get("succeeded"))
        elif kind == "CommandLine":
            if r.get("k", -1) >= 0:
                task(r).command_lines.append(r.get("cmd", ""))
        elif kind == "TaskParameter":
            pk = r.get("kind", "")
            name = r.get("name") or r.get("itemType") or ""
            items = _items(r.get("items"))
            if pk in ("SkippedTargetInputs", "SkippedTargetOutputs"):
                s = skipped.get((r.get("p", -1), r.get("t", -1)))
                if s is not None:
                    dest = s.inputs if pk == "SkippedTargetInputs" else s.outputs
                    dest.extend(i.spec for i in items)
            elif pk == "TaskInput" and r.get("k", -1) >= 0:
                task(r).inputs.setdefault(name, []).extend(items)
            elif pk == "TaskOutput" and r.get("k", -1) >= 0:
                task(r).outputs.setdefault(name, []).extend(items)
            elif pk in ("AddItem", "RemoveItem") and "t" in r:
                if pk == "AddItem":
                    target(r).added.setdefault(r.get("itemType") or name, []).extend(items)
                target(r).item_changes.append((pk == "AddItem", r.get("itemType") or name, items))
        elif kind == "Error":
            log.errors.append(BuildError(
                code=r.get("code", ""), file=r.get("file", ""), line=r.get("line", 0),
                project=r.get("project", ""), message=r.get("msg", "")))
        elif kind == "DumperError":
            log.dumper_errors.append("%s: %s" % (r.get("type", ""), r.get("msg", "")))
    return log


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: msbuild_events.py <msbuild.binlog> <events.ndjson> [MSBuild.exe]")
    used = dump_binlog(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    print(f"wrote {sys.argv[2]} (replayed with {used})")
