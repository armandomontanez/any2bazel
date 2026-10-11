# FUTURE: Windows build steps that should be Bazel modules

## Status

A proposal. No module here has been built. Everything in a "today" line exists
as a template under [`examples/powertoys/workspace-ui/bazel/`](../examples/powertoys/workspace-ui/bazel/)
(29 files, about 3,100 lines of Starlark and C#) and was exercised as far as
the guides say: [C++](REFERENCE-msbuild.md#what-has-been-exercised),
[C#](REFERENCE-msbuild-csharp.md#what-has-been-exercised),
[XAML](REFERENCE-msbuild-xaml.md#what-has-been-exercised). Where something was
not exercised, the entry says so.

## Why

An MSBuild project file says little about its own build. Most of it comes from
the SDK's `.props` and `.targets` and from the `.targets` of NuGet packages:
the resource compiler, MIDL, the WinRT projections, the XAML compilers, the
resource index, the copy of a runtime beside a program. The same dozen
mechanisms are in every Windows codebase, and none of them is specific to
winget-cli or PowerToys.

Today a migration copies the templates ("templates, not a library") and with
them, or without them, what it cost to get each one right: an option order, a
working directory, a list that must be `null` and not empty. A module would
hold that once.

Two rules from [BAZEL-RULES.md](BAZEL-RULES.md) apply to this list as well.
*BCR presence is not fitness*: what exists is listed below with what it does
not do. And a rule encodes a premise about where configuration lives: a module
that is to serve migrations must accept what the reference build did as
explicit input (a reference order, an exact option list), not only derive it.

## What exists (checked 2026-10-11)

The Bazel Central Registry listed 1,359 modules. Their names were searched for
dotnet, nuget, vcpkg, msvc, windows, winrt, winui, xaml, wpf, midl, resource,
wix, msix, vstest and pkg; the web and GitHub were searched for rulesets
outside it.

| What | Where | What it is | Used here |
|---|---|---|---|
| `rules_dotnet` 0.22.2 (the latest) | BCR | C# and F# rules, `nuget_repo`, `publish_binary` | yes, for 189 C# compiles; `publish_binary` not tried |
| `rules_cc` (latest 0.2.26) | BCR | C++ rules and the auto-configured MSVC toolchain | yes, resolved to 0.2.17 by Bazel 9.2.0 |
| `windows_resources` in `@bazel_tools//tools/res:win_res.bzl` | Bazel itself (present in 9.2.0) | `rc.exe` to `.res`, handed to the link through `CcInfo`, with its own toolchain type and an SDK-detecting repository rule | no. It runs `rc /nologo /fo<out> <file>`: no defines, include directories, language option or headers from `deps`, all of which MSBuild's `rc` command lines have here. The template `rc.bzl` was written without knowing of it and has the same shape |
| `windows_support` 0.4.1 | BCR | Windows SDK and MSVC runtime headers and libraries, downloaded, for building *for* Windows from Linux, macOS or Windows; described as still under construction. No tools (`rc`, `midl`, `mt`, `makepri`), no metadata | no |
| `rules_auto_dotnet` 0.3.5 | BCR | rules_dotnet targets generated from `.csproj` / `.fsproj`; its README calls it experimental and names no `-windows` framework, WPF or WinUI | no |
| `msix_packaging` | BCR | Microsoft's MSIX library as a C++ dependency; not rules | no |
| `rules_cc_resources` | BCR | binary files to C arrays; nothing to do with `.rc` | no |
| `rules_pkg` 1.3.0, `bazel_lib` 3.7.2 | BCR | archives and directory copies in general | no |
| `toolchains_msvc` | not in the BCR | described in a search result as fetching MSVC and defining rules_cc toolchains; repository not examined | no |
| `rules_msbuild` | not in the BCR | runs MSBuild inside Bazel actions; its README says out of date and non-functional and points to rules_dotnet | no |

Nothing was found, in the registry or outside, for vcpkg, for NuGet beyond
rules_dotnet's own rule, for MIDL, the C++/WinRT or C#/WinRT projections, the
XAML compilers, the resource index, WiX, or `vstest`.

## The gaps

Ordered by how much of a Windows codebase each one touches. "Home" is one of:
a **new module**; an **upstream change** to a ruleset that exists; **look
first** at something that exists and was not tried here; or **stays in the
migration**.

Counts: "PowerToys" is from its project files (353 projects: 110 C++, 243 C#);
"the slice" is the 33 projects that were built (17 C++, 16 C#).

### 1. The Windows SDK's tools and metadata, as a toolchain

- **Does:** `rc` (resource scripts), `mt` (manifests), `midl` (classic and
  `/winrt`), `mdmerge`, `makepri`, the SDK's contract metadata (`.winmd`), all
  of one SDK version, by label.
- **Today:** `windows_sdk.bzl` (122 lines, a repository rule over the installed
  SDK), `rc.bzl` (105), `midl.bzl` (101), `winrt.bzl` (135).
- **Seen in:** `.rc` in 76 of PowerToys' 110 native projects and 8 of the
  slice's 17; MIDL in every WinRT component and in winget-cli; a merged
  manifest in the runner.
- **Home:** a new module, the base the next three depend on. For `rc` alone,
  the shorter way is an upstream change to Bazel's own `windows_resources`
  (defines, include directories, options, headers of `deps`).
- **Must get right:**
  - Tools are declared inputs of one SDK version, the one the C++ toolchain is
    pinned to (`BAZEL_WINSDK_FULL_VERSION`). A second detection that picks
    another version is a silent difference.
  - `rc` and `midl` search the `INCLUDE` environment after their `/I`
    directories: the action needs the C++ toolchain's environment.
  - `midl` given the IDL path with forward slashes synthesized a different
    interface id for one interface. Spell it with backslashes.
  - Two metadata sets, not one: everything under `References` for `midl` (93
    contracts here), the contracts of `Platform.xml` for projections (33).
  - A `.winmd` is not reproducible (time stamp, module id). Compare what is
    generated from it.
  - `makepri` and `mt` also ship in the Microsoft.Windows.SDK.BuildTools NuGet
    package (used from there for the resource index), so a downloaded variant
    of the tools is possible; headers and libraries are what
    `windows_support` already downloads.

### 2. WinRT metadata and projections (C++/WinRT, C#/WinRT)

- **Does:** `.idl` to `.winmd`; the C++/WinRT projection of the SDK (once),
  of referenced components, and of a component being implemented
  (`-component`: `module.g.cpp`, `*.g.h`); the C#/WinRT projection sources.
- **Today:** `winrt.bzl` (135), `cppwinrt.bzl` (146), `dotnet/cswinrt.bzl` (69).
- **Seen in:** C++/WinRT headers in 97 of PowerToys' 110 native projects and
  15 of the slice's 17; 13 components in PowerToys (3 built); winget-cli; every
  C# project that references a component.
- **Home:** a new module.
- **Must get right:**
  - MSBuild regenerates the SDK projection in every project (1,358 identical
    headers each in winget-cli). It is one action for the whole build.
  - The include directory is called `Generated Files`: a space, which
    `includes` does not quote.
  - `module.g.cpp` includes the project's precompiled header: the component
    projection and gap 6 meet here.
  - Metadata must be buildable without the component's C++, or C# projects
    wait for native compiles they do not need.
  - `cswinrt.exe` writes nothing and reports nothing beyond `MAX_PATH`.
  - Projects filter and post-process projections (winget-cli prefixes every
    generated file and leaves one out): the rule needs filters and a hook.
  - CsWinRT's `IIDOptimizer` rewrites the compiled assembly afterwards. Not
    reproduced here.

### 3. NuGet as the reference build resolved it, native packages included

- **Does:** one repository per package from its feed with its hash; for each
  target, the files it takes (references, analyzers, headers, import
  libraries, tools, run-time files); framework packs as packages.
- **Today:** `scripts/nuget_assets.py` writes a lock from what MSBuild
  resolved; `dotnet/nuget_assets.bzl` (177) turns it into a repository.
- **Seen in:** every C# project; 13 packages used by the slice's *native* code
  (WIL, C++/WinRT, the Windows App SDK, WebView2), for which rules_dotnet's
  rule has nothing; 82 packages in the slice's lock.
- **Home:** a new module for the repository and the lock format. Producing a
  lock without MSBuild (NuGet's own resolution and asset selection) is a
  larger, separate problem, and the part rules_dotnet's `nuget_repo` attempts.
- **Must get right:**
  - Asset selection for platform frameworks (`net8.0-windows10.0.26100.0`);
    rules_dotnet drops assets filed under a framework it does not know.
  - Selections are per target: two projects take different files from one
    package version.
  - The hash of the package *file* (`.nupkg.sha512`); `contentHash` is another
    number for a signed package.
  - Feeds other than nuget.org (resolve the download address from the feed's
    index); `packages.config` next to `PackageReference`.
  - A package that contributes only build logic (`build/*.targets`) has
    nothing to download. Its effect is a rule, usually one of this list.
  - Sources a package has its consumers compile (the Windows App SDK's
    initializers).
  - The run-time closure of a program is what MSBuild copied beside it, not
    the union of its libraries' assets.

### 4. Windows App SDK and WinUI 3

- **Does:** the XAML compiler in two passes around the C# compile; the
  resource index (`.pri`) of libraries and programs; the self-contained
  runtime beside a program.
- **Today:** `dotnet/xaml_winui.bzl` (125) with `xaml/XamlCompile.cs` (191);
  `dotnet/pri.bzl` (87) with `xaml/MakePri.cs` (121).
- **Seen in:** 24 WinUI C# projects in PowerToys (256 `.xaml`) and 46 more
  that have a resource index for their strings; 4 built, with 4 indexes.
- **Home:** a new module on top of rules_dotnet and gaps 1 and 3.
- **Must get right:** all of it is in
  [the XAML guide](REFERENCE-msbuild-xaml.md#winui-3). In short: absent lists
  are `null`; the compiler runs from the project directory; the order of
  references shows in the output; the exit code says nothing; `#pragma
  checksum` carries an absolute path; the compiler is a .NET Framework
  program, an input no action declares; a program without its `.pri` opens to
  an exception; `makepri` names resources after staging paths; indexes are
  compared by content. It needs a compile's references as files, which a
  rules_dotnet target does not expose (gap 7).
- **Not exercised:** C++ XAML, packaged (MSIX) programs, the framework-
  dependent deployment.

### 5. WPF

- **Does:** `MarkupCompilePass1` and `2`, `ResourcesGenerator`, around the C#
  compile.
- **Today:** `dotnet/xaml_wpf.bzl` (167) with `xaml/WpfMarkup.cs` (211), a
  host for the task classes of the SDK's own `PresentationBuildTasks.dll`.
- **Seen in:** 4 WPF projects in PowerToys (43 `.xaml`); 2 built.
- **Home:** an upstream change to rules_dotnet, which already owns the SDK
  the tasks come from; a module otherwise.
- **Must get right:** the task takes the directory it was created in as the
  project's; pass 2 updates files pass 1 wrote; a second compile of the
  project feeds pass 2; `Resource` items go into `<Assembly>.g.resources`;
  the WindowsDesktop reference pack and the framework name in
  `runtimeconfig.json` (gap 7).

### 6. MSVC specifics of rules_cc

- **Does:** precompiled headers (`/Yc`, `/Yu`).
- **Today:** `pch.bzl` (130), which takes its command line from `cc_common` so
  that the toolchain's options are the same in both compiles.
- **Seen in:** 13 of the slice's 17 native projects.
- **Home:** an upstream change to rules_cc. A precompiled header is not only
  speed: without `/Yu`, `#include "pch.h"` is looked up beside the source, and
  shared or generated sources get another file or none.
- **Also upstream:** default defines that cannot be removed (`NOMINMAX`,
  `_WIN32_WINNT`); a target's static libraries placed before its `linkopts`;
  compile parameter files that `aquery` does not report; `textual_hdrs` on a
  binary. See [rules_cc on Windows](BAZEL-RULES.md#rules_cc-on-windows-the-msvc-toolchains-fixed-points).

### 7. rules_dotnet: where it differs from an MSBuild compile

- **Today:** worked around with `dotnet/csc_assembly.bzl` (204),
  `dotnet/apphost.bzl` (53), `dotnet/resx.bzl` (54), and two patches in
  [`patches/rules_dotnet/`](../patches/rules_dotnet/README.md).
- **Home:** upstream changes to rules_dotnet. The
  [table](REFERENCE-msbuild-csharp.md#rules_dotnet-what-it-reproduces-and-what-it-does-not)
  has each one: resource names (patched), `winexe` (patched), platform
  frameworks, which assemblies of a reference pack are passed, the
  WindowsDesktop pack, `runtimeconfig.json`, one selection of a package
  replacing another at run time, `.resx` conversion that keeps line ends.
- **New from the XAML work:** a compile's references as files, for tools that
  read them (both XAML compilers); a host executable with the GUI subsystem
  and the assembly's icon in a directory laid out like MSBuild's.
  `publish_binary` exists in rules_dotnet and was not tried.

### 8. Parts of Visual Studio beyond the compiler

- **Does:** Spectre-mitigated libraries, the C++ unit-test framework,
  `vstest.console` as a test runner.
- **Today:** `msvc_toolset.bzl` (109), `vstest.bzl` (75).
- **Seen in:** winget-cli (Spectre), a PowerToys test DLL (484 tests under
  `bazel test`).
- **Home:** a small new module, or part of an MSVC toolchain module if one
  becomes usable (`toolchains_msvc` was not examined). Tests hosted by VSTest
  for C# have no template yet.

### 9. vcpkg

- **Does:** the project's own manifest, baseline, overlay triplets and ports
  installed in a repository rule; one `cc_library` per port, from vcpkg's own
  database of installed files.
- **Today:** `vcpkg.bzl` (133).
- **Seen in:** winget-cli; PowerToys (two ports, one of them in 75 projects).
- **Home:** a new module. None was found.
- **Must get right:** the manifest verbatim (another version or triplet makes
  the comparison meaningless); the static-CRT triplet; short build trees
  (`MAX_PATH`); MSBuild's integration offers every library to every link, and
  a wildcard names nothing: dependencies are per binary. It is a build inside
  a repository rule and so not hermetic.

### 10. COM interop and Win32 metadata generators

- **Does:** `tlbimp` for `<COMReference>` (an interop assembly from a type
  library); CsWin32 run as a generator.
- **Today:** nothing. Interop assemblies are copied from the reference tree;
  two PowerToys projects that run CsWin32 as a build task are not emitted.
- **Home:** a small new module. Not built here, so the list of what it must
  get right is short: MSBuild takes the type library from the machine's
  registry, and a rule needs a declared file instead.

### 11. Program and product directories

- **Does:** files of many targets under the names the reference build gave
  them, with an executable inside that `bazel run` starts.
- **Today:** `layout.bzl` (93): a destination-to-label dictionary (about 900
  entries for a self-contained WinUI program) and symbolic links.
- **Home:** look first at `bazel_lib` (`copy_to_directory`) and `rules_pkg`.
  Neither was tried. What the template needed: explicit destinations, links
  instead of copies, and an executable in the directory.
- **Not built:** one directory for a whole product, which PowerToys' runner
  needs to find its modules.

### 12. Packaging

MSIX (`makeappx`, package identity, sparse packages), WiX installers, signing.
None of it was attempted. No ruleset was found; `msix_packaging` in the BCR is
the library such a rule could use.

### Stays in the migration

- `csc_assembly.bzl`: a compile with exactly the reference build's command
  line. A parity tool; a ruleset should not need it once gap 7 is closed.
- `msbuild_config.bzl`, `msbuild_defines.bzl`: MSBuild's generated analyzer
  configuration and defines, restated per project by the emitter.
- Rules for a project's own generators (a version header, a resource script
  made from translations): the `snapshots` of a migration.

## Order

If these were built: 1 (the SDK's tools) and 3 (NuGet) first, since 2, 4 and
5 take their tools and inputs from them; then 2, which nearly every native
project needs; 6 upstream in parallel; then 4, 5 and 7; 9, 8, 10 as projects
need them; 11 and 12 once a whole product is the goal.

## What a module would have to show

What the templates were held to: for the same inputs, the same output as
MSBuild, checked on files and not only on command lines (projection headers
and XAML compiler output byte for byte, a resource index by content, a binary
by exports, imports and resources), and a program that starts. And on two
unrelated codebases: winget-cli and PowerToys disagreed in ways that mattered
(one post-processes its projections, the other builds every program into one
directory).

## Sources

- Registry: <https://github.com/bazelbuild/bazel-central-registry/tree/main/modules>
- <https://github.com/bazelbuild/bazel/tree/master/tools/res>
- <https://github.com/hermeticbuild/windows_support>
- <https://github.com/bazel-contrib/rules_dotnet>
- <https://github.com/scottpledger/rules_auto_dotnet>
- <https://github.com/ClangTools/rules_cc_resources>
- <https://github.com/microsoft/msix-packaging>
- <https://github.com/samhowes/rules_msbuild>
