# Reference build: MSBuild, C# and rules_dotnet

Read [REFERENCE-msbuild.md](REFERENCE-msbuild.md) first: detection, the reference
build, the binary log and the report are the same. This guide covers what is
different for managed code: recovering compiler command lines, NuGet, the files
MSBuild generates, the WinRT interop chain, and what `rules_dotnet` can and
cannot reproduce.

Exercised on winget-cli (22 C# compiles) and PowerToys (199 non-XAML C#
compiles). Numbers and open ends are at the [end](#what-has-been-exercised).

## The loop

```sh
# 1. Reference build with a binary log, as in REFERENCE-msbuild.md.
msbuild Foo.sln -restore -p:Configuration=Release -p:Platform=x64 -bl:msbuild.binlog

# 2. Model + report. --design-time recovers the command line of every C#
#    project the build skipped as up to date (see below).
python3 extract_msbuild.py msbuild.binlog <ref> ref.model.json --report ref.report.json --design-time

# 3. What NuGet resolved, as a lock for Bazel.
python3 nuget_assets.py ref.report.json <ref> <workspace>/nuget_lock.json --fetch-packs

# 4. Templates into the workspace (examples/*/workspace/bazel), then BUILD files.
python3 emit_dotnet.py ref.report.json <workspace>/nuget_lock.json <workspace> --reference-root <ref>

# 5. Build, compare command lines, compare what was generated.
bazel build //...
bazel aquery 'mnemonic("CSharpCompile", //...)' --include_param_files --output=jsonproto > aquery.json
python3 extract_bazel.py aquery.json <workspace> bazel.model.json
python3 diff.py ref.model.json bazel.model.json any2bazel.json | python3 triage.py
python3 check_generated.py ref.model.json bazel.model.json any2bazel.json
```

`<ref>` is the tree the reference build ran in; `<workspace>` is a clone that
never held MSBuild output. The emitter is a starting point, not an oracle: it
writes what the report says, and `diff.py` plus `check_generated.py` decide
whether that was enough. Both found real defects in emitter output and in
`rules_dotnet` during the two migrations (they are listed below as they come
up).

## Compiler command lines of projects the build skipped

C# has no tracking logs. A project MSBuild finds up to date runs no `Csc` task,
so an incremental binary log has the command line of the projects that happened
to be rebuilt and nothing for the rest (14 of winget's 22 in the first log).

`--design-time` runs a second, compile-free pass, the one an IDE uses to learn a
project's compiler options. For every project instance of the real build (same
global properties, including `TargetFramework` of an inner build) it runs

    -t:PrepareResources;Compile
    -p:DesignTimeBuild=true -p:BuildingInsideVisualStudio=true -p:BuildProjectReferences=false
    -p:SkipCompilerExecution=true -p:ProvideCommandLineArgs=true

through a generated traversal project and reads the `CommandLineArgs` the `Csc`
task returns. Three details matter:

* Task outputs are only in a **binary** log; the pass writes one and replays it.
* Without `PrepareResources` the `/resource:` arguments are missing.
* The pass adds `/nowarn:2008`; that is removed.

The result is not trusted on its word. Every project the real build DID compile
is compared with its design-time command line, and the report says how that
went:

```json
"completeness": {"design_time": {"status": "verified", "checked_against_real_compiles": 22,
                                 "mismatched": {}, "used_for": 14}}
```

`verified` on winget (22 of 22 equal). On PowerToys 198 of 199 were equal; the
one that was not is a project whose real build ran the XAML markup compiler,
which a design-time build does not. `status: mismatch` names the projects and
what differed; a mismatched project's design-time line is not used.

## The C# part of the report

`targets.<name>.csharp`, one per compiled assembly (a multi-targeted project is
`Name[net48]`, `Name[net8.0-windows...]`):

| Field | What it is |
|---|---|
| `assembly`, `target`, `target_framework`, `runtime_identifier` | identity |
| `defines`, `options` | canonical (see [how C# is compared](#how-c-is-compared)) |
| `sources.hand_written` | repo files |
| `sources.generated` | files the build wrote, by `kind`: `assembly_info`, `target_framework_attribute`, `global_usings`, `test_entry_point`, `cswinrt_projection` (one entry per directory), `xaml_or_generator_output`, `designer`, `generated` |
| `sources.external` | sources a package ships (the test SDK's `Program.cs`) |
| `references` | `packs` (count per framework pack), `pack_files` and `pack_roots` (which files, from where), `nuget` (package, version, path), `projects`, `other` |
| `analyzers` | the same split, plus `sdk` for analyzers in the SDK directory itself |
| `additional_files`, `analyzer_configs` | with origin: `repo`, `generated`, `sdk`, `nuget` |
| `resources` | logical `name`, `path`, and `from` (the `.resx` it was converted from) |

`targets.<name>.winrt` on a native project that produces `.winmd`: its IDL,
MIDL options, references, namespace depth for `mdmerge`, the merged file and
where the build copies it.

## NuGet: lock MSBuild's answer, do not re-resolve it

`nuget_assets.py` resolves nothing. It records:

* **compile assets** from the csc command lines: exactly the package files each
  compile was given;
* **run-time files** from what MSBuild copied beside each program (the build's
  copy steps, which are in the report), together with the run-time and native
  assets of the project's `obj/project.assets.json`;
* package hashes and feeds from that assets file.

`nuget_assets.bzl` (template) downloads each package by integrity and exposes
`@nuget//<id>/<version>:<framework>` as a `rules_dotnet` `import_library` with
that selection. rules_dotnet's own `nuget_repo` re-derives the selection from
the package layout and drops assets filed under a framework it does not know
(`lib/net8.0-windows10.0.19041.0/`); a lock of MSBuild's answer has no such gap.

Things that were learned the hard way and are now in the script:

* **Integrity is the hash of the `.nupkg` file**, which NuGet leaves beside an
  installed package as `<id>.<version>.nupkg.sha512`. The `sha512` in an assets
  file is computed with the signature removed, so for any signed package it is
  not the hash of what a feed serves, and a download checked against it fails.
* **Displaced packages are not dependencies.** A package whose assembly the
  target framework already contains (`Microsoft.Win32.Registry` on .NET 8) is
  in the assets file but not on the compile line: MSBuild's conflict resolution
  dropped it. Depending on it from Bazel makes rules_dotnet drop the
  framework's copy instead. The lock lists it under `displaced` and leaves it
  out.
* **Selections are per target.** Two projects of one framework can take
  different files from one package: a project that embeds the C#/WinRT runtime
  references the package's host shim but not `WinRT.Runtime.dll`. Merged, the
  second project got both and failed with an ambiguous call. Each distinct
  selection is its own target (`<framework>`, `<framework>_alt1`, ...).
* **Framework packs read from the machine are locked as the packages they
  are.** WindowsDesktop and ASP.NET reference packs come from
  `dotnet\packs\`, the .NET Framework reference assemblies from the Visual
  Studio targeting pack. Each is a copy of a NuGet package;
  `--fetch-packs` finds or downloads it, hashes it, and compares every file the
  build read with the package's, byte for byte (winget: 142 + 166 + 117 files
  identical; PowerToys: 173 + 53). `Microsoft.NETCore.App.Ref` and
  `NETStandard.Library` are left to rules_dotnet, except for a target the exact
  rule compiles (below).
* **References that displace a framework assembly are marked.**
  `Microsoft.NETCore.App.Ref` has a facade `WindowsBase.dll`; a project that
  references WindowsDesktop gets the real one and MSBuild drops the facade.
  rules_dotnet passes its whole pack unless a *direct* dependency is *named*
  like the assembly and has a higher version. The lock lists such files under
  `overrides`, the repository rule gives each a target
  (`<framework>.as.WindowsBase`), and the emitter depends on it. Without this
  both files are passed and `DependencyObject` "claims it is defined in
  WindowsBase, but it could not be found".
* **What a program loads is what MSBuild put beside it**, and that is more
  than the assets file's run-time list. MSTest's adapter is not a NuGet asset
  of its package (the assets file has a placeholder): the package's `.props`
  add it as a `<Reference>` and its `.targets` copy two more assemblies. The
  Windows SDK projection (`WinRT.Runtime.dll`, `Microsoft.Windows.SDK.NET.dll`)
  arrives as the run-time half of a framework reference. All of them are in the
  build's copy steps, so the lock takes a package file copied into the
  program's output directory as `runtime` (an assembly), `resources` (a
  satellite assembly in its culture directory) or `native`. A package's own
  launcher (`testhost.exe` beside `testhost.dll`) is left out: to the .NET host
  it would be a second assembly of the same name.
* **Run-time files belong to programs, not libraries.** MSBuild deploys the
  program's own restore graph; a library deploys nothing. rules_dotnet keys
  what a program loads by library name and version, so a library's selection
  of a package would replace the program's (a test lost `WinRT.Runtime.dll`
  that way). A library's selection is therefore compile-only, and when a
  package has several selections each gets its own library name.
* **Package files named anywhere else are exported too**: a tool a build step
  runs (`cswinrt.exe`), files a step copies in, `.winmd` named in a response
  file, paths in MSBuild's generated analyzer config.

## The emitter

`emit_dotnet.py` writes one `BUILD.bazel` per project directory (only over
files carrying its marker), `bazel/dotnet/BUILD.bazel` and
`bazel/dotnet/msbuild_defines.bzl` (shared), `bazel/winmd/BUILD.bazel` (WinRT
metadata the C# side needs), `emit_dotnet.summary.json` (what happened to each
target and why) and `emit_dotnet.any2bazel.json` (config to merge: how the two
builds' generated files correspond).

Every C# target of the report ends in exactly one bucket, printed at the end:

| Bucket | Meaning |
|---|---|
| emitted | a `csharp_library` / `csharp_binary`, or `exact_csharp_assembly` (see gaps) |
| excluded | the project compiles XAML (`<assembly>.g.resources` among its resources), or depends on one that does |
| not emitted | something the emitter cannot express; the first line of the reason is printed, all are in the summary. A dependent of an unemitted target is unemitted too, and says so |

What it does with the parts of a compile:

* **Options** become the attributes rules_dotnet has (`langversion`, `nullable`,
  `allow_unsafe_blocks`, `warning_level`, `nowarn`, `treat_warnings_as_errors`,
  `warnings_as_errors`, `warnings_not_as_errors`, `keyfile`,
  `generate_documentation_file`); the rest goes to `compiler_options`. Compiler
  defaults are stated, because rules_dotnet's differ (`/warn:3`, csc's is 4).
* **Framework defines** (`NET8_0_OR_GREATER`, `WINDOWS10_0_17763_0_OR_GREATER`,
  ...) are written once per target framework into `MSBUILD_FRAMEWORK_DEFINES`.
  A platform framework (`net8.0-windows10.0.26100.0`) is not a rules_dotnet
  framework: the target gets `net8.0`, and the platform is these defines plus
  the references, both of which the diff compares.
* **Files MSBuild writes for every project** (assembly info, the
  target-framework attribute, global usings, the test platform's entry point,
  the generated analyzer config, a generated manifest) are restated with
  `write_file` from the reference build's content. A path into a NuGet package
  inside the analyzer config (the metadata CsWin32 is pointed at) becomes a
  label through `msbuild_editorconfig`, so the Bazel build does not read the
  NuGet cache of whoever ran the reference.
* **`.resx`** goes through `resx_resources`; files a `.resx` links to are its
  `linked` inputs.
* **The C#/WinRT projection** becomes a `cswinrt_projection` target from the
  project's `cswinrt.rsp`.
* **A generator of the project's own** is not guessable. `--generated-map
  FILE` tells the emitter which rule produces a generated directory:

  ```json
  [{"match": "/Generated Files/CsWinRTPragma/",
    "srcs": [":{target}_cswinrt_pragma"],
    "load": {"//bazel/winget:cswinrt_pragma.bzl": ["cswinrt_pragma_sources"]},
    "starlark": "cswinrt_pragma_sources(name = \"{target}_cswinrt_pragma\", srcs = [\":{target}_cswinrt\"], ...)",
    "bazel_output": "{target}_cswinrt_pragma.cs"}]
  ```

## rules_dotnet: what it reproduces and what it does not

rules_dotnet 0.22.2 on Bazel 9, Windows. It needs `--enable_runfiles` (a
program finds its assemblies through the runfiles tree). Its compile action is
`CSharpCompile` and always uses a parameter file, so
`aquery --include_param_files` has the whole command line; a library with
`internals_visible_to` has a second, `/refonly` action, which the extractor
drops.

Everything below was hit on one of the two projects, except the two rows marked
(source), which are read from rules_dotnet's code and were not run into. Each has a template or an emitter rule that closes it without
patching rules_dotnet:

| rules_dotnet does | MSBuild does | Consequence | Closed by |
|---|---|---|---|
| names every embedded resource `<assembly>.<path>` | `<RootNamespace>.<path>`, or `LogicalName` | `ResourceManager` finds nothing when the assembly name is not the root namespace (`PowerToys.ManagedCommon` / `ManagedCommon`) | `csc_assembly` |
| passes every assembly of the .NET Framework reference pack (339) | the ones referenced, plus facades (221) | two of the extra ones declare `Microsoft.Windows`; `Windows.Foundation` inside `namespace Microsoft...` no longer binds | `csc_assembly` |
| `project_sdk = "web"` passes every analyzer of the ASP.NET pack | the ones the project enables | the configuration-binding generator runs and the compile fails (CS9137) | `csc_assembly` |
| passes its whole base pack beside a pack that displaces part of it | drops the displaced assembly | two `WindowsBase.dll`; types "could not be found" | `overrides` in the lock |
| drops package assets under a framework it does not know (source) | uses them | missing references on `-windows` frameworks | the lock |
| has no WindowsDesktop reference pack | reads it from the SDK | no WPF / WinForms references | pack through the lock |
| declares `winexe` and compiles with `/target:exe` | `/target:winexe` | a GUI program opens a console | `/target:winexe` in `compiler_options` |
| `warning_level` accepts 0-5 | current SDKs pass `/warn:8` and up | analysis fails | `compiler_options` |
| defines `NETSTANDARD*_OR_GREATER` for every framework | does not | a warning in the diff; harmless unless code tests them | accepted |
| lets two selections of one package overwrite each other at run time (keyed by library name and version) | deploys the program's own selection | a program loses assemblies to a library's compile-only selection | one library name per selection; run-time files on programs only |
| writes a `runtimeconfig.json` naming `Microsoft.NETCore.App` only (source) | names WindowsDesktop when used | a program that needs the WindowsDesktop shared framework would not find it; not observed in the suites that ran | open |

### `csc_assembly`: the exact command line

`csc_assembly.bzl` runs the same compiler from the same rules_dotnet toolchain
and passes what its attributes say and nothing else: references and analyzers
as files, resources with their names, options as the reference build spelled
them. `exact_csharp_assembly` wraps the result in rules_dotnet's public
`import_library`, so `csharp_library` / `csharp_binary` targets depend on it
like on any other library; in the other direction a rules_dotnet library is
just a file to reference. Its action is a `CSharpCompile` with a parameter
file, so it is extracted and compared like rules_dotnet's own.

`msbuild_csharp.rules_dotnet_gaps()` decides which targets need it (the first
three rows of the table), and both the lock builder and the emitter use that
one function: such a target takes even `Microsoft.NETCore.App.Ref` from the
lock. The emitted target says why:

```starlark
# rules_dotnet's own rules cannot reproduce this compile:
#   embedded resource ManagedCommon.CommonResources.resources: rules_dotnet names every resource `<assembly>.<path>`
exact_csharp_assembly(
    name = "ManagedCommon",
    assembly_name = "PowerToys.ManagedCommon",
    resources = {":ManagedCommon.CommonResources_resx": "ManagedCommon.CommonResources.resources"},
    ...
```

It is for those cases, not a replacement: it has none of rules_dotnet's
handling of run-time dependencies or executables. An executable compiled this
way is built and can be referenced (by its tests), but is not runnable under
Bazel.

## Templates

In `examples/*/workspace/bazel/`:

| File | Does |
|---|---|
| `dotnet/nuget_assets.bzl` | repository of the lock's packages and selections |
| `dotnet/csc_assembly.bzl`, `csc_wrapper.bat` | the exact compile and its wrapper |
| `dotnet/dotnet_sdk.bzl` | files of the toolchain's SDK by label (the platform analyzers, their global configs) |
| `dotnet/resx.bzl`, `resx/` | `.resx` to `.resources`, the way MSBuild's `GenerateResource` does it |
| `dotnet/msbuild_config.bzl` | MSBuild's generated analyzer config with package files by label |
| `dotnet/cswinrt.bzl` | `cswinrt.exe` over metadata to C# projection sources |
| `winrt.bzl` | `midl /winrt` + `mdmerge` to `.winmd` |
| `windows_sdk.bzl` | the SDK's contract metadata (all of it for `midl`; the platform's 33 contracts for `cswinrt`) and tools |

## How C# is compared

`ignore.include_csharp: true` in `any2bazel.json` turns it on.

Assemblies are paired by **assembly name and base target framework**
(`net8.0-windows10.0.26100.0` pairs with `net8.0`); target names do not matter
and no `target_map` is needed. Per pair:

| Kind | Compared | Reference-only | Bazel-only |
|---|---|---|---|
| `missing_cs_src` / `extra_cs_src` | source set, per assembly | error | warning |
| `defines_diff` | `/define` | error | warning |
| `flags_diff` | options, after canonicalization | error | error if it changes a compiler default (`/nullable:enable` where the reference has none), else warning |
| `cs_refs_diff` | references by assembly file name; package version where both sides say it; for a framework pack, which pack (its version is a toolchain fact) | error | warning; **error if one name comes from two files** |
| `cs_analyzers_diff` | analyzers and source generators | error | warning |
| `cs_resources_diff` | resource names, additional files, analyzer configs | error | - |
| `toolchain_diff` | `dotnet_sdk_version`, `pack:<id>` | error unless listed in `ignore.toolchain` | |

Canonicalization of a csc command line (`canonicalize_csc.py`): `/x` and `-x`,
long and short names (`/r`, `/d`, `/t`), lists split, `1591` and `CS1591`
unified (an analyzer id is kept as written: `/warnaserror-:CSWINRT1028` does
not apply to `CsWinRT1028`, which is how a compile broke), last-wins for
single-state options with the compiler's default elided (`/warn:4`,
`/nullable:disable`, `/platform:anycpu`, each checked against csc itself),
outputs and reporting dropped, `/optimize` and `/debug` treated as build type.
An option that names a file (`/win32manifest:`) is compared by which file it
is.

**Generated sources** have different paths in the two builds. `ignore.source_map`
pairs them: the emitter writes the entries (`emit_dotnet.any2bazel.json`), one
token per MSBuild-written file and one per generated directory. Bazel outputs
are named as `bazel-bin/...` in config (the configuration directory under
`bazel-out` is not stable); matching ignores case. A generated source of the
reference with no rule is not an error but is reported as not compared.

## `check_generated.py`: what a collapsed directory hides

`source_map` collapses a generated directory to one token on each side, and
then the diff can only say "both compile the projection". Which files are in
it, and what is in them, is invisible to a comparison of command lines. After a
build, `check_generated.py` pairs the files behind every token and compares
their text (encoding, line ends and a final newline are not differences), and
compares the `.resources` both builds embed under one name byte for byte.

It exists because the diff converged on builds that were wrong:

* winget's MSBuild target leaves `Module.cs` out of the C#/WinRT sources it
  compiles; the Bazel rule did not, and the platform analyzer rejected every
  WinRT call.
* One GUID differed in a projection: `midl` had synthesized a different IID for
  an interface (see field notes). The assembly compiled, and could not have
  talked to any other build of the same component.
* A projection directory was empty: `cswinrt.exe` had written nothing to a path
  beyond `MAX_PATH`, without an error.
* Seven `.resources` files differed by a few bytes: line ends in multi-line
  strings.

## The WinRT interop chain

    X.idl --midl /winrt--> Unmerged/X.winmd --mdmerge--> <Namespace>.winmd
          --cswinrt.exe--> C# projection sources --csc--> assembly

MSBuild runs the first two steps in the native project and the third in every
C# project that references it. Under Bazel the metadata is a target
(`winrt_winmd`) that needs none of the component's C++ to be built, and the
projection a target (`cswinrt_projection`) whose output directory is listed in
`srcs`.

* `-input <SDK version>` in `cswinrt.rsp` means the contracts of
  `Platforms\UAP\<version>\Platform.xml` (33), not everything under
  `References` (93). With all 93 the projection has extra generic
  instantiations.
* A project may post-process the projection. winget prefixes every generated
  file with `#pragma warning disable` and compiles the copies; that is a
  project rule plus a `--generated-map` entry.
* .NET Framework references the `.winmd` directly; there is no projection.
* The `.winmd` itself is not reproducible (time stamp, module version id). The
  projection generated from it is, and that is what is compared.
* CsWinRT's `IIDOptimizer.exe` rewrites the compiled assembly after the
  compile. That step is in the report (`steps`) and is not reproduced.

## Tests

How a managed test is run differs by framework, and the report says which
(`targets.<name>.test`):

* **MSTest on Microsoft.Testing.Platform** (PowerToys): the test project is a
  program. MSBuild writes its entry point (`TestPlatformEntryPoint.cs`,
  `SelfRegisteredExtensions.cs`), which the emitter restates, and the target is
  a `csharp_test`: its exit status is the result.
* **xunit / NUnit / MSTest under VSTest** (winget): the test assembly is loaded
  by a host (`dotnet test`, `vstest.console`), which finds the adapter beside
  it. Such a project is also compiled as a program, but the `Main` the test SDK
  gives it is empty. As a `csharp_test` it would pass without running a test,
  so the emitter writes a `csharp_binary` and notes that it needs a host. No
  template runs these yet.

What a test program needs at run time is the lock's business
([above](#nuget-lock-msbuilds-answer-do-not-re-resolve-it)): the files MSBuild
copied beside it. Compare the run with the reference build's program, not with
"it passed": same number of tests, same results.

On Windows, expect `MAX_PATH`. A program's runfiles directory is
`<output base>\execroot\_main\bazel-out\<config>\bin\<package>\<target>\<tfm>\<assembly>.dll.bat.runfiles`,
and Bazel's test wrapper does not start one whose path reaches about 250
characters. With the emitter's naming (target = project name, package =
project directory) a project called
`Microsoft.CmdPal.Ext.PerformanceMonitor.UnitTests` is far beyond that.

See [what has been exercised](#what-has-been-exercised) for which suites ran.

## Field notes

* **`midl`: spell the IDL path with backslashes.** Given
  `a/b/X.idl` instead of `a\b\X.idl`, midl 10.0.26100 synthesized a different
  IID for one default interface of winget's configuration API. Same file, same
  options, same references; found only because the projection is compared.
* **`cswinrt.exe` and `MAX_PATH`**: no error, no file. Keep its output
  directory short (`cswinrt_projection(out = "cswinrt.cs")`).
* **`/noconfig` is ignored in a response file** (CS2023). It must be on the
  command line itself.
* **MSBuild reads `.resx` with `XmlTextReader`**, which does not normalize line
  ends: a multi-line string keeps the `\r\n` of the file. The converter must do
  the same or every such resource differs.
* **An analyzer diagnostic id is case-sensitive** on the command line; a
  compiler one (`CS1591`) is not.
* **MSBuild's `.NET Framework` reference assemblies** come from the installed
  targeting pack even when the `Microsoft.NETFramework.ReferenceAssemblies`
  package is restored. The two are byte-identical (117 files compared).
* **Long outputs repeat the project name**: `<package>/<target>/<tfm>/<assembly>`
  under an output base is close to `MAX_PATH` for a project called
  `Microsoft.CmdPal.AdaptiveCards.IncrementalRendering.UnitTests`. Use a short
  output base (`--output_user_root`) for large solutions.

## What has been exercised

| | winget-cli | PowerToys |
|---|---|---|
| C# compiles in the reference build | 22 | 199 (the projects that compile no XAML and depend on none that does) |
| Emitted and built by Bazel, from a tree without MSBuild output | 22 | 167 |
| ... through `csc_assembly` | 3 | 16 |
| `diff.py` | converged, 0 errors (19 warnings, all `NETSTANDARD*` defines rules_dotnet adds) | converged, 0 errors (151 warnings, all `NETSTANDARD*` defines rules_dotnet adds) |
| `check_generated.py` | identical: 54 generated trees and resource sets, 426 files, 0 mismatched | identical: 750 generated trees and resource sets, 867 files, 0 mismatched |
| Excluded (XAML) | 0 | 1 |
| Not emitted | 0 | 31 |

**Tests.** winget's four managed test assemblies are built and not run: all are
hosted by VSTest (their generated `Main` is empty, so the emitter writes a
`csharp_binary`, not a `csharp_test` that would pass without running anything),
and the three unit-test ones load native code that is not built here.

PowerToys' test projects are MSTest on Microsoft.Testing.Platform, programs that
run themselves; 69 of the 167 emitted targets are `csharp_test`. Leaving out UI
tests and fuzzers, 58 unit-test suites were run both ways. The reference build's
programs run 6,384 tests with no failure. Under `bazel test`:

| | Suites | Tests |
|---|---|---|
| Same tests, same results as the reference program | 12 | 1,140 |
| Run, with failures the reference does not have | 4 | 20 of 510 fail. 14 report "class not registered": they activate PowerToys' native WinRT components, which are not built here. The other 6 are in the DSC suite, in the fixture whose first test fails that way; not looked into further |
| Cannot be started | 42 | Bazel's Windows test wrapper cannot load the runfiles (26) or reports a path that is too long (12), or the .NET host cannot find a file of its manifest (4) |

The 42 are a property of the layout, not of the build. Every one has a runfiles
directory path of 250 characters or more; the 16 that start are at 248 or less.
`<output base>\execroot\_main\bazel-out\<config>\bin\<package>\<target>\<tfm>\<assembly>.dll.bat.runfiles`
names the project three times, and the longest project names are over 60
characters. A shorter output base does not buy enough; shorter target names
would.

Getting the first suite to start took three corrections that no compile and no
diff shows, each found by running it: MSTest's adapter is not a NuGet asset of
its package but a `<Reference>` its `.props` add; the rest of its files are
copied by its `.targets`; and rules_dotnet lets a library's selection of a
package replace the program's. The run-time closure is now what MSBuild copied
beside each program, taken from the build's copy steps.

Not exercised: XAML (WPF, WinUI) and packaging; a `<COMReference>` (the interop
assembly MSBuild generates from a registered type library); CsWin32 run as a
build task; a VSTest-hosted suite under `bazel test`; publishing
(self-contained, AOT, single file); Debug or ARM64; `dotnet build` as the
driver; F# and VB. CsWinRT's `IIDOptimizer` post-compile step is reported and
not reproduced.

The commands and per-target detail are in
[CASE-winget-cli-migration.md](CASE-winget-cli-migration.md) (section 10) and
the two example READMEs.
