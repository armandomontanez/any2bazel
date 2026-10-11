# Reference build: MSBuild

Supporting context for migrating **from** MSBuild (Visual Studio solutions and
projects). The migration process itself lives in [SKILL.md](../SKILL.md); this
file only covers what is unique to MSBuild as the reference build.

**Status: new.** C/C++ compile and link parity uses the main loop. C# is
compared per assembly (sources, options, references, analyzers, resources) and
is opt-in; it has its own guide,
[REFERENCE-msbuild-csharp.md](REFERENCE-msbuild-csharp.md), which also covers
`rules_dotnet` and the WinRT interop chain. Packaging and test execution are
extracted and reported, not diffed. See
[what has been exercised](#what-has-been-exercised), the
[case study](CASE-winget-cli-migration.md), and the working overlays in
[`examples/winget-cli/`](../examples/winget-cli/) and
[`examples/powertoys/`](../examples/powertoys/).

Two companions to read with it:
[REFERENCE-msbuild-playbook.md](REFERENCE-msbuild-playbook.md) has the order
of work that held up, the methods for getting unstuck, and an index of
failures by message; XAML, resource indexes and programs that start are in
[REFERENCE-msbuild-xaml.md](REFERENCE-msbuild-xaml.md).

## Detecting it

A `.sln` or `.slnx` at or near the repo root, with `.vcxproj` and/or `.csproj`
projects. Windows only: the reference build needs Visual Studio or the Build
Tools, and the Bazel side needs an MSVC-compatible toolchain (`cl.exe` or
`clang-cl`). There is no GNU translation layer.

Run commands from Git Bash and write MSBuild switches with a dash (`-p:`,
`-t:`, `-bl:`). Bash rewrites `/p:Foo=bar` into a path.

## Scope traps

When [confirming scope](../SKILL.md#confirm-scope), build the reference and run
the extractor first: its report lists what this build actually contains. Surface
these before proceeding.

- **Generated code is everywhere in C++/WinRT projects.** Every project that
  imports the C++/WinRT NuGet package generates projection headers into its
  intermediate directory, and IDL-authored components also generate sources.
  Look for `generated_sources` and `steps` in the report.
- **Dependencies fetched or built during the build**: NuGet restore, and vcpkg
  running `vcpkg install` from inside MSBuild. Each needs a Bazel-side home
  ([External dependencies](#external-dependencies)).
- **XAML, MSIX and WiX projects.** XAML compilation (WPF, WinUI), packaging
  (`.wapproj`) and installers (`.wixproj`) are not migrated: packaging projects
  are listed with role `package`, and a C# project that compiles XAML is put in
  its own bucket by the emitter. C# without XAML is in scope
  ([REFERENCE-msbuild-csharp.md](REFERENCE-msbuild-csharp.md)).
- **Projects that consume each other's outputs by path** rather than by
  reference, ordered only by solution-level dependencies. They show up as
  `copy` and `exec` steps, as libraries no project reference explains, and in
  `undeclared_generated_inputs`: generated files a project reads from a project
  it does not reference. Each of those is a dependency Bazel must be told.
- **Tests that need machine state** (an installed product, registered packages,
  elevated rights). The report says how each test is hosted, not what it needs
  at run time; read the CI definition for that.

## Building the reference

Extraction reads a **binary log** of a real build. Add `-bl:` to the command the
project itself uses (its CI definition is the authority):

```bash
msbuild <solution> -restore -p:RestorePackagesConfig=true -m \
    -p:Configuration=Release -p:Platform=x64 -bl:msbuild.binlog
```

- One binlog per configuration and platform you intend to migrate. Start with
  the one CI ships.
- **An incremental build is enough**; a clean build is not required. Commands
  for sources MSBuild skipped as up to date are recovered from the project's
  tracking logs (below). The build does have to *succeed*: a project the build
  never reached has nothing to recover.
- Avoid `-t:Rebuild` unless the project documents it. A project-specific
  `Clean` can delete state the build does not recreate (winget-cli's removes
  its vcpkg tree).
- Building a single project outside its solution changes its output paths and
  can disable NuGet imports. Pass `-p:SolutionDir=<dir>\` or build through the
  solution (`-t:<ProjectName>`).
- If a build fails on a missing *generated* header while MSBuild reports the
  generating target as up to date, the generator's stamp file has outlived its
  output. Delete the stale stamp (for C++/WinRT, the project's
  `*.vcxproj.cppwinrt_*.rsp`) and build again.
- A single project that fails on a missing header or package may simply need
  another project built or restored first: the dependency was never declared.
  Build that project, note it, and carry on; it is a finding, not an obstacle.
- **Where the checkout lives matters.** vcpkg run from MSBuild builds inside
  `<repo>\vcpkg_installed`, and a long root path overflows `MAX_PATH` in the
  ports' CMake checks. The file tracker does not record reads under `%TEMP%`,
  so a checkout there gives incomplete read logs (`generated_inputs`
  under-reports; command lines are unaffected).

## Extracting the reference model

```bash
python3 "$SKILL_DIR/scripts/extract_msbuild.py" msbuild.binlog <repo_root> \
    model.msbuild.json --report msbuild.report.json
```

This replays the binlog through MSBuild itself (no .NET SDK or extra package is
needed; a small logger is compiled once with the compiler MSBuild ships) and
writes `msbuild.binlog.events.ndjson` next to it. Re-running on the `.ndjson`
skips the replay. Use `--msbuild <MSBuild.exe>` if the newest installed MSBuild
is older than the one that produced the binlog: a binlog can only be read by
the same or a newer MSBuild.

The extractor prints where each compile command came from:

| source | meaning |
|---|---|
| `binlog` | the build ran the compiler and logged the command line |
| `tlog` | the build skipped the source as up to date; the command is the tracking-log entry MSBuild compared against in this build |
| `tlog_unverified` | from a tracking log, but this build stopped before re-checking the project. **Possibly stale**; rebuild if it matters |
| `skipped_target` | C# compile skipped as up to date; the source set comes from the target's declared inputs, and there are no flags. Extract with `--design-time` to recover the command line instead |
| `design_time` | C# compile skipped as up to date; the command line is the one a design-time pass computed, cross-checked against the projects that did compile (`completeness.design_time`) |

It **exits non-zero if any evaluated source has no recovered command** and lists
them. That is deliberate: a model of part of the build would make everything
missing from it look fine. Fix the build and re-run; `--allow-incomplete` is for
looking at a partial result, not for migrating from one.

## The report

Read `msbuild.report.json`, not the model, when writing BUILD files. The model
is megabytes of argv; the report is what a BUILD author needs.

| section | content |
|---|---|
| `build` | entry point, configuration, platform, global properties, environment variables the build read, build errors |
| `toolchain` | MSVC toolset version, platform toolset, Windows SDK version, target architecture; `all` lists every combination in use if projects differ |
| `solution_configuration` | each project's configuration under this solution configuration, and whether it builds at all |
| `externals` | one entry per external root: kind, version, the include dirs / libs / tools / build files used from it, who uses it, for NuGet the package hash and feed, for vcpkg the installed ports |
| `undeclared_generated_inputs` | per target, directories of generated files it reads that belong to a project it does not reference |
| `targets.<name>.compile.variants` | sources grouped by identical canonical flags, largest group first; later groups say how they differ from the first |
| `targets.<name>.generated_inputs` | generated files the compiles read (from the read tracking log), by directory: produced by this project, or by which other one, and whether that one is referenced |
| `targets.<name>.headers` | every file of the repository the compiles READ besides their sources (from the read tracking log). No project file lists them, and Bazel refuses a header nobody declared |
| `targets.<name>.external_reads` | what the compiles read from package directories: files per package, and for a vcpkg tree the ports those files belong to |
| `targets.<name>.include_environment` | directories of the `INCLUDE` variable that are not the toolchain's. Searched after `/I`, and on no command line (see the field notes) |
| `targets.<name>.resources` | one entry per resource script: its options, the `.res` it writes, and the files it read |
| `targets.<name>.optimization` | the optimization options, as given (`/O2 /GL /Gy`, `/LTCG /OPT:REF`). The digest leaves them out as the build type; a Bazel build that is to be the same build needs them |
| `targets.<name>.external_headers` | `/external:*` flags and the `EXTERNAL_INCLUDE` directories: the warning policy for headers the project does not own |
| `targets.<name>.dead_include_dirs` | include directories on the command line that do not exist on disk |
| `targets.<name>` (other keys) | kind, role, output, project references, deps with origin, `libraries_offered_by_wildcard`, `link_flags`, `precompiled_header`, `generated_sources`, `steps`, `test`, `pri` (the resource index, see the XAML guide) |
| `completeness` | unrecovered sources and possibly-stale projects |

Paths in the report are repo-relative, or start with a token naming an external
root: `@nuget/<id>`, `@vcpkg/<triplet>`, `@msvc`, `@winsdk`, `@vs`, `@dotnet`.

## Extracting the Bazel action graph

```bash
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    --features=-compiler_param_file --include_param_files \
    [--config=<name>] -c opt --output=jsonproto > aquery.json
python3 "$SKILL_DIR/scripts/extract_bazel.py" aquery.json <repo_root> model.bazel.json
```

- **Do not pass `--features=-linker_param_file`.** rules_cc's MSVC toolchain
  rejects it ("unconditionally implies feature 'linker_param_file'").
  `--include_param_files` makes aquery report the param file's content instead,
  and the extractor splices it in.
- Use `-c opt` for a Release reference and `-c dbg` for Debug, or the project's
  `--config`. Record the arguments in `any2bazel.json`'s `bazel_args`.
- Add `CSharpCompile` to the mnemonics when C# is in scope.

### Toolchain

MSBuild resolves the compiler and Windows SDK from the machine. Do the same on
the Bazel side, **pinned to the versions the reference used** (`toolchain` in
the report), in `.bazelrc`:

```
common --repo_env=BAZEL_VC="C:\Program Files\Microsoft Visual Studio\18\Community\VC"
common --repo_env=BAZEL_VC_FULL_VERSION=14.51.36231
common --repo_env=BAZEL_WINSDK_FULL_VERSION=10.0.26100.0
```

Without the pin Bazel picks the newest installation, which is often not the one
that built the reference. The diff reports a mismatch as `toolchain_diff` and
does not converge until it is fixed or accepted in `ignore.toolchain`.

rules_cc's MSVC toolchain adds flags to every compile and they **cannot be
switched off** (`--features=-default_compile_flags` fails analysis):
`/DNOMINMAX /D_CRT_SECURE_NO_DEPRECATE /D_CRT_SECURE_NO_WARNINGS /bigobj /Zm500
/EHsc /wd4351 /wd4291 /wd4250 /wd4996 /D_WIN32_WINNT=0x0601`, plus `/std:c++17`
on C++ sources. MSBuild defines none of those macros, so undo them once, in
`.bazelrc`, rather than accepting them as differences:

```
# The variable holds the whole flag. MSBuild sets no _WIN32_WINNT and lets the
# SDK headers choose; 0x0601 (Windows 7) hides newer declarations.
common --repo_env=BAZEL_WIN32_WINNT=/U_WIN32_WINNT
build --copt=/UNOMINMAX
build --copt=/U_CRT_SECURE_NO_DEPRECATE
build --copt=/U_CRT_SECURE_NO_WARNINGS
```

If the project does set `_WIN32_WINNT`, put its value in the variable instead
(`/D_WIN32_WINNT=0x0A00`). The `/U` lines are not cosmetic: a source that
defines `NOMINMAX` itself is a C4005 redefinition, which is an error under
`/WX`, and `NOMINMAX` removes `min`/`max` macros the project may use. Each
compile then prints a harmless D9025 "overriding" warning.

The warning suppressions and `/bigobj` stay; they are extra flags on the Bazel
side and the diff tolerates those. A project's `/std:` or C runtime flag later
on the line overrides the toolchain's, and the diff compares the effective one.
Put the project's `/std:` in `cxxopts`, not `copts`: `copts` also reaches C
sources, which the reference compiles without it.

## `any2bazel.json` for MSBuild

```json
{
  "bazel_args": ["-c", "opt"],
  "align_shared_libs": true,
  "target_map": { "UndockedRegFreeWinRT": "src/Xlang/UndockedRegFreeWinRT:winrtact.dll" },
  "ignore": {
    "include_map": [
      {"from": "external/+http_archive+nuget_wil",
       "to": "@nuget/microsoft.windows.implementationlibrary"},
      {"from": "bazel-out/x64_windows-opt/bin/external/+http_archive+nuget_wil",
       "to": "@nuget/microsoft.windows.implementationlibrary"},
      {"from": "external/+vcpkg_install+vcpkg/installed", "to": "@vcpkg"},
      {"from": "src/AppInstallerSharedLib/x64/Release/Generated Files", "to": "@winrt-projection"},
      {"from": "bazel-out/x64_windows-opt/bin/winrt_platform", "to": "@winrt-projection"}
    ],
    "source_map": [
      {"from": "src/WinGetServer/WinGetServer_c.c", "to": "@midl/WinGetServer_c.c"},
      {"from": "bazel-out/x64_windows-opt/bin/src/WinGetServer/WinGetServer_c.c",
       "to": "@midl/WinGetServer_c.c"}
    ]
  }
}
```

- **`align_shared_libs`** (set it): a DLL is a deliverable with its own `.def`,
  subsystem and delay-load flags. With it on, DLLs are name-aligned like
  executables: a missing one is `missing_target` and its link flags are diffed.
- **`target_map`** keys are MSBuild project names; a name shared by several
  projects is the repo-relative project path, and a multi-targeted project is
  `Name[framework]`. A DLL or executable usually needs an entry, because the
  project name (`UndockedRegFreeWinRT`) and the Bazel label of its output
  (`...:winrtact.dll`) rarely coincide.
- **`include_map`**: the reference side already spells external roots as tokens
  (`@nuget/<id>`, `@vcpkg/<triplet>`). Map Bazel's `external/<repo>` (and its
  `bazel-out/.../external/<repo>` twin) to the same token. A generated include
  directory needs both spellings mapped to a token of your choosing: every
  MSBuild project has its own `<IntDir>Generated Files`, so several reference
  directories map to the one Bazel action that replaces them.
- **`source_map`** reconciles generated sources: a path in the source tree or an
  intermediate directory in MSBuild, a path under `bazel-out` in Bazel. The
  token replaces the whole path, so map each generated file to its own token
  unless the two sides group them differently.
- **`ignore.include_prefixes`** is where a `dead_include_dirs` entry goes: a
  directory that does not exist contributes nothing and needs no Bazel
  counterpart. Do not use it for a directory that does exist.
- Include rules (`include_map`, `include_prefixes`) match case-insensitively
  for MSBuild models: a path recovered from a tracking log is upper-case when
  it could not be found on disk.
- **`exclude_targets`** is how a partial migration states its scope. List the
  reference targets not attempted yet; `excluded` in the output counts them, so
  a slice can converge without pretending to be the whole build.
- **`include_csharp`** (default `false`): also diff C# assemblies. Leave it off
  while migrating the C++; C# targets are then listed under `excluded`.
- **`ignore.toolchain`**: toolchain facts (`msvc_version`, `winsdk_version`,
  `target_arch`) a reviewer accepts as differing.

## How MSVC command lines are compared

Both sides are read in the MSVC dialect, separately from the GNU rules:

- `/x` and `-x` are the same option, as are `/D NAME` and `/DNAME`.
- Paths compare case-insensitively and relative paths resolve against the
  directory the command ran in (MSBuild compiles from the project directory).
- **Defines and single-state options resolve the way the compiler does: the
  last one wins.** A toolchain default followed by the project's value compares
  as the project's value; `/MD ... /MT` is `/MT`.
- A state equal to the compiler's default is not a flag. MSBuild writes
  `/Zc:wchar_t /Zc:forScope /Gd /fp:precise /GS` on every line; leaving them out
  is the same build. `/TP` and `/TC` count only when they contradict the file
  extension.
- Dropped as mechanics: output paths, `/nologo`, `/FS`, `/MP`, `/FC`, PCH
  plumbing (`/Yc /Yu /Fp`), `/analyze*`, `/external:W*`, diagnostics options.
  Ignored as build type: `/O*`, `/Z7 /Zi`, `/GL`, `/Gy`, `/RTC*`; on the link
  line `/DEBUG`, `/OPT`, `/LTCG`.
- Everything else is compared, including unknown flags, the C runtime, `/std`,
  `/EH`, `/GR-`, `/permissive-`, `/Zc:*`, `/utf-8`, `/guard`, `/Qspectre`,
  `/sdl` and warning options. Never add these to `ignore` to get to green.

Link lines follow the same rules, checked against `link.exe` itself:

- Option names are case-insensitive, and single-state options (`/SUBSYSTEM`,
  `/MACHINE`, `/MANIFEST`, `/MANIFESTUAC`, `/DEF`, `/ENTRY`, `/DYNAMICBASE`,
  `/NXCOMPAT`, ...) resolve last-wins. rules_cc puts `/SUBSYSTEM:CONSOLE` on
  every link; a project's `/SUBSYSTEM:WINDOWS` after it is a GUI image, and
  before it is not.
- `/DYNAMICBASE` and `/NXCOMPAT` are what the linker does anyway and are not
  flags; `/DYNAMICBASE:NO` is. `/MANIFEST` **is** a flag: a bare `link.exe`
  writes no manifest. `/MANIFEST /MANIFEST:EMBED` is one decision, `EMBED`.
- Search paths are mechanics, with one exception. A `lib\spectre\<arch>`
  directory on the reference link line means the binary links the
  Spectre-mitigated C runtime and STL; it compares as
  `/LIBPATH:@msvc/lib/spectre` and must be matched (`@msvc//:spectre_libs` in
  the example workspace). `/Qspectre` on the compile line does not imply it.
- A library is a dependency when the command line names it. A `.lib` that is
  only an *input* of the Bazel link (a whole directory offered through
  `/LIBPATH`) is not evidence that it was linked.

Kinds specific to this frontend:

| kind | meaning and fix |
|---|---|
| `toolchain_diff` | the two builds resolved different MSVC or SDK versions, or architectures. Pin the Bazel toolchain |
| `tu_variant_count` (warn) | the reference compiles a source with more distinct flag sets than Bazel does (one file built into several projects with different flags). Check that sharing one compile is intended |
| `missing_cs_src` / `extra_cs_src` | C# source set of an assembly differs (`include_csharp` only) |
| `cs_refs_diff`, `cs_analyzers_diff`, `cs_resources_diff` | C# references, analyzers, or resources and compile inputs differ; see the C# guide |

## External dependencies

The rule: **the Bazel build must pass with the MSBuild tree moved away.** Do not
`glob()` into `packages/`, `vcpkg_installed/`, or a project's output or
intermediate directories; each of those is a dependency that needs its own
boundary. `externals` in the report says what each one is.

| report `kind` | what it is | Bazel side |
|---|---|---|
| `msvc` | compiler, CRT, STL, ATL/MFC, Spectre libraries, the unit-test framework under `VC\Auxiliary` | the pinned host toolchain. Libraries read from `lib\spectre\<arch>` (shown under `libs`) mean the build links Spectre-mitigated runtimes; that directory must come first in the link search path |
| `winsdk` | headers and import libraries (through the toolchain environment); tools (`midl`, `rc`, `mt`, `makepri`) and contract `.winmd` files (by path) | toolchain for headers and libraries. For tools and winmds, a repository rule that locates the **pinned** SDK version and exposes them as labels, so actions declare them as inputs |
| `nuget` | a restored package: headers, libraries, tools, and build logic imported from its `build\` directory | `http_archive` (`type = "zip"`) per package from the recorded `source` feed with the recorded `integrity`, exposing only the `include_dirs`, `sources`, `libs` and `tools` the report lists (`sources`: files the package has its consumers compile, such as the Windows App SDK's initializers). A package listing only `build_imports` contributed MSBuild logic: reproduce its effect (flags, generated files), not the file |
| `vcpkg` | a tree vcpkg built during the MSBuild build | a Bazel-owned install from the project's own manifest, baseline, overlay triplets and overlay ports, verbatim. One `cc_library` per port from the `ports` list. Not a registry substitute and not the nearest NuGet package: different versions and build flags make the diff meaningless |
| `generated` | a file the build produced under the repo (untracked, or in an intermediate directory) | a Bazel action; see below |
| `unknown` | none of the above | an undeclared host dependency. Find out what it is before migrating |

Each recipe exists as a working file under
[`examples/winget-cli/workspace/bazel/`](../examples/winget-cli/workspace/bazel/)
and [`examples/powertoys/workspace/bazel/`](../examples/powertoys/workspace/bazel/):

| file | replaces |
|---|---|
| `windows_sdk.bzl` | paths into `Windows Kits`: SDK metadata and tools (`midl`, `rc`, ...) by label, for one named SDK version |
| `msvc_toolset.bzl` | parts of Visual Studio beyond the toolchain: Spectre-mitigated libraries, the C++ unit-test framework, the location of `vstest.console.exe` |
| `vcpkg.bzl` | `vcpkg install` inside MSBuild: the project's manifest into a repository, one target per port |
| `nuget/*.BUILD.bazel` | a restored package directory, as an `http_archive` |
| `cppwinrt.bzl`, `midl.bzl`, `rc.bzl` | generators and the resource compiler, as actions with declared outputs. `cppwinrt_component` is `cppwinrt -component` for a project that implements WinRT classes |
| `pch.bzl` | MSVC precompiled headers (`/Yc`, `/Yu`), which rules_cc does not have |
| `layout.bzl` | the shared output directory: files of many targets under the names the reference build gave them, runnable with `bazel run` |
| `vstest.bzl` | the CI step that runs a test DLL |

Copy and adapt them; they are templates, not a library. Which of them should
be one, and what exists already, is in
[FUTURE-windows-bazel-modules.md](FUTURE-windows-bazel-modules.md).

NuGet facts are read from what restore left on disk: `integrity` is NuGet's own
SHA-512 in the form Bazel's `integrity` attribute takes, and `source` is the feed
index. Resolve the download URL from that feed's `PackageBaseAddress`; do not
assume nuget.org. A package restored with `packages.config` has no recorded
feed: take it from the repo's `nuget.config`.

vcpkg's MSBuild integration adds `<tree>\lib\*.lib` to every link line. A
wildcard names nothing, so those libraries are reported as
`libraries_offered_by_wildcard` and are **not** dependencies. Declare per binary
what it uses; the linker reports what is missing.

Libraries an object pulls in through `/DEFAULTLIB` directives (the CRT,
`uuid.lib`, `#pragma comment(lib, ...)`) never appear on a command line. They are
resolved by the toolchain's library path on both sides.

## The native emitter

```bash
python3 "$SKILL_DIR/scripts/emit_cc.py" msbuild.report.json nuget_lock.json <workspace> \
    --reference-root <tree the reference build ran in> [--generated-map hooks.json]
```

`emit_cc.py` restates each C++ project of the report as a `cc_library` or
`cc_binary`, in the project's directory. It is a starting point, not an
authority: run the diff on the result, and `check_outputs.py` after building.

| MSBuild | what it writes |
|---|---|
| sources, defines, options | `srcs`, `local_defines`, `copts`, from the compile command lines. `/MT` becomes `features = ["static_link_msvcrt"]` |
| include directories | `copts` (`/I<dir>`), not `includes`: MSBuild does not hand them to referencing projects |
| headers | the files the compiles read (`headers` in the report), listed in `srcs`; one of another package by label, exported from there |
| package directories | dependencies: the vcpkg ports and NuGet packages whose headers were read, and the directory alone (`@vcpkg//:include`, `cc.headers`) where the command line names one nothing was read from |
| precompiled header | `msvc_precompiled_header`, and `/Yu` on the target |
| `.rc` | `windows_resources`, with the files the script read |
| MIDL 3 + `cppwinrt -component` | `winrt_winmd`, `cppwinrt_component`; the Windows SDK projection once for all (`//bazel/cppwinrt:platform`) |
| link | system libraries by name in `linkopts`, other projects as labels, `/DEF:` as `win_def_file`, manifests as inputs |
| output name | a program or DLL is named after its file (`PowerToys.Interop.dll`), a static library after its project |

A program-specific build step has no rule the emitter could know: a version
header written by a target, a resource script made from translations by a
PowerShell script. A file such a step generated is **copied from the reference
tree** into the workspace, at the path the build read it from, and listed
under `snapshots` in `emit_cc.summary.json`. The build works; the file is a
frozen copy until a rule replaces it. `--generated-map` names such rules:

```json
[{"match": "version/Generated Files/", "deps": ["//src/common/version:generated"]}]
```

`match` is a fragment of the generated file's path, `deps` the libraries that
provide it as a header, `file` its label where it is an input by itself (a
resource script). Count the snapshots: each is a step still to migrate.

What it cannot express is listed per target under `unhandled`, and that target
is not written. It also writes `emit_cc.any2bazel.json`: the `target_map`,
`include_map`, `source_map` and `source_dir_map` entries that follow from its
naming, to merge into `any2bazel.json`. None of them names a directory of the
machine the reference was built on: the model already spells a path below a
package with the package's token (`@nuget/<id>/include/x.cpp`), so the entries
only say where Bazel has the same package.

NuGet packages come from the same lock as for C# (`nuget_assets.py`): a C++
project has no assets file, so what it takes from a package is what its command
lines name. The lock gives each such package `cc.headers` and one target per
library, and exports the tools and metadata files a build step used.

## Where MSBuild differs from Bazel

**Configuration.** A solution configuration (`Release|x64`) is not uniform: it
maps each project to its own configuration and platform and can exclude
projects (`solution_configuration`). Flags come from the project, imported
property sheets, `Directory.Build.props`, NuGet package props, and the command
line. The extracted command lines already reflect all of that; restate what is
common as one `.bzl` constant rather than per target. Extract one model per
configuration, and express differences with `--config` and `select()`.

**Generated files.** Typical sources, all visible as `steps`:
MIDL (`.idl` to headers, stubs or `.winmd`), cppwinrt (`.winmd` to headers and
`*.g.cpp`), resource compilation (`.rc` to `.res`, a link input), and custom
targets that write version or resource headers. A `write` step for a `.rsp`
file carries its lines: that is the command line of the tool that reads it. A
`target` step with `ran: false` was up to date in this build but still lists
its declared inputs and outputs. Each becomes a Bazel action whose tool comes
from a NuGet or SDK repository. A `copy` step marked `up_to_date` lists files
MSBuild was to deploy and found in place: another project building into the
same directory had copied them (see "Programs" in the XAML guide).

Two shapes need care:

- **Files generated into the source tree.** Classic MIDL writes `Foo.h`,
  `Foo_c.c` and `Foo_s.c` beside `Foo.idl`, and *other projects* then compile
  those files by relative path. Nothing declares that they need the first
  project's MIDL step; the solution's build order makes it work. The report
  marks such a source as generated. In Bazel it is an output of the generating
  target, consumers name its label, and `source_map` aligns the two paths.
- **The same generated tree, once per project.** The CppWinRT package makes
  every project project the whole Windows SDK into its own
  `<IntDir>Generated Files` (winget-cli: the same 1,358 headers for each C++
  project that imports the package). The output depends only on the tool and
  the SDK metadata, so it is one Bazel action that every consumer depends on.

**Sources and headers shared without a library.** MSBuild lets a project list
another directory's sources (`<ClCompile Include="..\Other\x.cpp">`) and compiles
them with its own flags; the report shows each one as a separate variant of
that source. Keep that shape: export the files and list them in each consumer's
`srcs`, so each is compiled with that consumer's flags. A shared-items project
(`.vcxitems`) has no output at all; its headers reach consumers only through an
include directory, and Bazel rejects them as undeclared inclusions until a
header-only `cc_library` owns them. Project-local include directories are
`copts`, not `includes`: MSBuild does not hand them to referencing projects.

**Precompiled headers.** `/Yc` and `/Yu` are compared as mechanics, and rules_cc
has no MSVC PCH support. Compiling without one is NOT the same build, only
slower. With `/Yu"pch.h"` the compiler does not open `pch.h`: it takes
everything up to `#include "pch.h"` from the `.pch`. Without it, that directive
is an ordinary include, looked up beside the source first. So a source one
project shares with another (`<ClCompile Include="..\Other\trace.cpp">`, common
in PowerToys) picks up the OTHER project's `pch.h`, with other contents, and a
generated source that sits in an output directory (`module.g.cpp` of
C++/WinRT) finds none. `pch.bzl` is a rule for it: `msvc_precompiled_header`
runs the `/Yc` compile through the C++ toolchain, and the target's own compiles
use the result (`/Yu`, `/Fp$(execpath ...)`, `additional_compiler_inputs`).
Both must be given the same options, or the compiler rejects the `.pch`.
`precompiled_header.not_using` lists the sources that opted out; they are
compiled apart. Do not add `/FI"pch.h"`.

**Warnings in headers the project does not own.** MSBuild marks toolchain,
NuGet and vcpkg directories as external through the `EXTERNAL_INCLUDE`
environment variable and passes `/external:W<n>` (often with
`/external:anglebrackets`). The differ ignores these as diagnostics, but with
`/WX` they decide whether the code compiles: a warning inside a third-party
header becomes an error. Repeat the flags from `external_headers` in `copts`.

**C runtime.** `/MD` is the default on both sides. For a `/MT` reference use
`features = ["static_link_msvcrt"]`, not `copts = ["/MT"]`: the feature also
switches the default library the link pulls in.

**Resources.** rules_cc has no resource compiler. A project's `.rc` files
(`other_actions: ResourceCompile`) become `.res` link inputs through a rule such
as `rc.bzl`; without one the binary links fine and has no version information.
(Bazel ships a `windows_resources` rule of its own in
`@bazel_tools//tools/res:win_res.bzl`. It gives `rc` no defines, include
directories or options, and a project's resource scripts usually need all
three; it was not used here.)

**Linking.** Project references to static libraries link transitively. A DLL
project usually also has a `.def` file, embedded resources, a manifest and
delay-load flags; `link_flags` shows them, with paths repo-relative. In Bazel a
DLL is `cc_binary(name = "x.dll", linkshared = True, win_def_file = ...)`.

A static library can carry other libraries: `<Lib><AdditionalDependencies>`
makes the librarian copy their members in, and whoever links the library gets
them without naming them. The report lists them as `deps` of the static
library. A `cc_library` cannot contain another library; it hands them on
(`linkopts`, `deps`), which reaches the same links. Miss it and a link fails on
a symbol the reference link line never names either.

**Output layout.** Projects often write into one shared
`<Platform>\<Configuration>\` tree, copy files into each other's output
directories, and find siblings at run time by relative path. In Bazel that
layout is runfiles or an explicit packaging rule; `copy` steps and
`project_references` marked `ReferenceOutputAssembly: false` (build order only)
show what is expected next to what.

**Tests.** MSBuild builds test projects; something else runs them. `test` in
the report gives the framework and the evidence for it:

| framework | hosted as | Bazel |
|---|---|---|
| `cppunittest` | a DLL loaded by `vstest.console.exe` | `cc_binary(linkshared = True)` depending on the framework (`@msvc//:cppunittest`), plus a test rule that runs it with vstest from the pinned Visual Studio instance (`vstest.bzl`). Exercised: see `examples/powertoys/` |
| `unknown` on an executable (Catch2, GoogleTest, custom `main`) | the executable | `cc_test`. Set `BAZEL_SH`; on Windows `cc_test` needs a shell toolchain. Not exercised here |
| `mstest`, `xunit`, `nunit` | a .NET assembly run by vstest or as its own executable | rules_dotnet. Not exercised here |
| `libfuzzer` | a fuzz target | out of scope |

`unknown` means the project is a test by name or property but nothing on its
command lines names a framework. That is the usual case when the framework
comes from vcpkg, whose libraries reach the link through a wildcard: check the
sources.

- **A test DLL is not a test target to `aquery`.** Add a `target_map` entry
  pairing the Bazel `cc_binary` with the reference test project. That entry is
  what makes the differ compare it as that test; without it the test sources
  are matched by name only and the diff can converge having compared nothing
  (it warns: `test_binary_count`, `extra_target`).
- Test binaries are compared at compile level. Their link lines are not diffed.
- **Compare test results, not just the build.** Run the reference binary the
  way CI does and run the Bazel one; the same tests should be discovered and
  the same ones should pass. A test that fails on both sides, or flakes on
  both, is a property of the test and the machine.
- Test data is usually copied next to the test binary (`copy` steps into its
  output directory, often flattening a `TestData\` tree) and found through the
  binary's own path. It becomes `data`, and the lookup needs a runfiles path or
  an argument.

Set `include_tests` only after production parity.

## Field notes

- A compile error under Bazel that MSBuild does not have is almost always a
  missing flag or define, not a source problem. Do not edit sources to fix it.
- `/permissive-` changes name lookup. Without it, C++/WinRT and WRL headers can
  produce ambiguous-symbol errors that look unrelated.
- An include directory with a space (`Generated Files`) needs quoting that
  `includes = [...]` does not provide; pass it through `copts` or avoid the
  space in the Bazel output layout.
- Build bottom-up (leaf libraries first). Errors in a top-level target bury the
  first real one.
- `diff.py` prints JSON to stdout. In PowerShell, `>` writes UTF-16; use Git
  Bash, or expect the readers here to cope only with `aquery.json`.
- **The `INCLUDE` variable is part of the build.** The compiler and the resource
  compiler search it after their `/I` directories, and no command line shows
  it. A relative entry is taken from the tool's working directory and need not
  exist to matter: `#include "../../../common/version/version.h"` from a
  directory only three levels deep resolves through `<project>\<anything>`.
  PowerToys has such an entry (`PreventSdkUapPropsAssignment`, the name of a
  property, leaked into the variable) and sources that only compile because of
  it. The report lists these entries (`include_environment`).
- **A project library can hide a system library of the same name.**
  `#pragma comment(lib, "version.lib")` asks for the SDK's `version.lib`; with
  PowerToys' own `Version.lib` on the link line the linker considers the
  request met and never opens the SDK's. The reference still links because the
  library that needs it was archived with it (see Linking).
- **A source file can be an include.** `#include <common/SettingsAPI/settings_helpers.cpp>`
  makes a `.cpp` a header of another project. Listed in `srcs`, rules_cc would
  compile it; it belongs in `textual_hdrs` of a `cc_library`.
- **Option order is lost in the digest, and sometimes matters.**
  `/std:c++latest` implies `/permissive-` for whatever `/permissive` came before
  it. State `/std:` first.
- **NuGet records two hashes.** `<id>.<version>.nupkg.sha512` is the hash of the
  package file; `contentHash` in `.nupkg.metadata` leaves the signature out and
  is a different number for a signed package. Only the first can check a
  download.
- **The same function, another DLL.** Windows ships most of kernel32 a second
  time as API-set libraries (through `WindowsApp.lib`), and a symbol comes from
  whichever import library the linker reaches first. Bazel puts a target's
  static libraries before its `linkopts`, MSBuild after: a binary can import
  `GetFileType` from `kernel32.dll` where the reference imports it from
  `api-ms-win-core-file-l1-1-0.dll`. Both load the same code.
  `check_outputs.py` reports it as a note.

## What has been exercised

On one machine: Bazel 9.2.0, rules_cc 0.2.17, MSVC 14.51.36231, Windows SDK
10.0.26100.0.

- **Extraction**: winget-cli (`AppInstallerCLI.sln`, Release|x64, MSBuild 18.10,
  toolset v145: 41 targets, every evaluated source recovered from an
  incremental build), PowerToys `src/common/UnitTests-CommonUtils` (same
  toolset) and PowerToys `src/common/version` (MSBuild 17.14, v143).
- **Build and diff**, from trees without MSBuild output: one winget static
  library, one winget DLL and one PowerToys test DLL. Each converges with 0
  errors and 0 warnings, and 24 negative controls across them break
  convergence.
- **Recipes**: NuGet (`http_archive` with NuGet's hash), vcpkg (manifest install
  in a repository rule), the C++/WinRT platform projection (byte-identical to
  MSBuild's), classic MIDL, the resource compiler, Spectre-mitigated libraries,
  CppUnitTest under `bazel test` (same 484 tests, same results as the reference
  DLL).

- **The native emitter**, on a 33-project slice of PowerToys (the runner, two
  WinUI apps, two WPF apps, a native utility and what they reference): all 17
  C++ projects emitted and built, among them the runner (`PowerToys.exe`),
  three module DLLs and three C++/WinRT components. The slice converges with 0
  errors, C# included, and `check_outputs.py` finds the same exports, imported
  functions and resources in all 8 binaries.

C# under rules_dotnet, with `midl /winrt`, `mdmerge` and the C#/WinRT projection,
is covered in [REFERENCE-msbuild-csharp.md](REFERENCE-msbuild-csharp.md); XAML
(WinUI 3 and WPF), resource indexes and runnable program directories in
[REFERENCE-msbuild-xaml.md](REFERENCE-msbuild-xaml.md).

Not exercised: a whole native solution (the emitter has seen 17 projects of
PowerToys' 110); a Catch2 or GoogleTest executable under `cc_test`; C++ XAML;
MSIX and WiX packaging; Debug or ARM64 configurations; `clang-cl`; builds
driven by `dotnet build`; and anything on Linux beyond the differ's own tests.
