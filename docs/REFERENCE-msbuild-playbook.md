# Reference build: MSBuild, the playbook

What to do in which order, how to get unstuck, and what each failure meant.
Everything here happened while taking winget-cli and PowerToys through the
MSBuild frontend; the mechanisms are explained in the three guides
([C++](REFERENCE-msbuild.md), [C#](REFERENCE-msbuild-csharp.md),
[XAML and programs](REFERENCE-msbuild-xaml.md)) and this file points into them.

Read [Order of work](#order-of-work) and [Methods](#methods) before starting.
Come back to the [symptom index](#symptom-index) when a step fails: look the
message up before reasoning about it.

Where a row says the extractor or an emitter *does* something, the fix is in
the tool: extract and emit again. If the symptom is still there, it is a new
variant, and the row tells you where to look.

## Order of work

1. **Pick a slice a person can see working.** One program someone can start,
   and everything it references. Take the transitive closure of
   `<ProjectReference>` from the project files (C# and C++ alike), write it as
   a solution filter (`.slnf`: `{"solution": {"path": "X.slnx", "projects":
   [...]}}`) and build *through the filter*, so the solution's configuration
   mapping still applies. Print a reference that cannot be resolved to a file
   (`$(...)` left in the path) instead of skipping it. PowerToys has 353
   projects; a 33-project slice (two WinUI programs, two WPF programs, the
   runner, a native utility) went through every mechanism in these guides. The
   closure also says early how cheap the next program is: Color Picker needed 2
   projects on top of the 31 already there.
2. **Build the reference clean, in a clone of its own, with `-bl`.** Not in a
   working checkout, not under `%TEMP%` (the file tracker records no reads
   there), and with a short root path (vcpkg's build trees overflow `MAX_PATH`).
   Keep that tree: the checks compare with its files and the emitters copy
   generated files from it. A second clone gets the Bazel overlay and never
   holds MSBuild output.
3. **Settle disk and paths before the first Bazel build.** Put
   `startup --windows_enable_symlinks` (needs Developer Mode) and
   `--enable_runfiles` in `.bazelrc`. Without links, every rules_dotnet tool
   and program gets a runfiles tree with the whole .NET SDK copied into it; two
   solutions took 89 GB of output base here before the setting was on.
4. **Native code first, leaf libraries first.** Every UI program loads native
   DLLs (interop components, module DLLs), and managed tests that activate
   them fail with "class not registered" until they exist. Projects share
   property sheets, so the first few decide most of what the emitter has to
   know.
5. **Emit; do not write BUILD files by hand.** Extract, lock, then
   `emit_cc.py` and after it `emit_dotnet.py` (it reads the C++ emitter's
   summary to find the native DLLs a program takes along). A fix to a BUILD
   file is a fix to the extractor or an emitter, with a test, or a
   `--generated-map` entry. Three numbers in the summaries are the remaining
   work: `unhandled`, `snapshots`, `not_laid_out`.
6. **Converge the diff.** Merge both `emit_*.any2bazel.json` into
   `any2bazel.json`, query with `--features=-compiler_param_file
   --include_param_files`, triage before reading entries.
7. **Compare what a command line does not show**: `check_generated.py`
   (generated sources, resources), `check_outputs.py` (XAML compiler output,
   resource indexes, exports and imports of binaries).
8. **Start the program, next to the reference build's.** Same arguments, same
   machine, and compare what appears ([below](#the-reference-program-defines-works)).
9. **Break it on purpose**, then **check what you ship**
   ([below](#a-check-that-has-never-failed-has-not-been-tested)).

## Methods

### Replay the reference before forming a theory

The log has the exact command of every compile and link, with its working
directory. When Bazel's version of a step fails, run the reference's command
again in the reference tree, writing to a scratch file. It succeeds; then
exchange one input or one option at a time for Bazel's until it stops.

An unresolved `GetFileVersionInfoSizeW` in the runner's link produced three
theories in a row (dead-code elimination, `/GL` with `/LTCG`, a link option);
each cost a build and each was wrong. Replaying the reference link and swapping
libraries one by one took minutes: the link broke when Bazel's
`ApplicationUpdate.lib` replaced the reference's, and listing the members of
both showed that MSBuild had archived an import library *into* that static
library. The same replay with the options bisected found that C4596 came from
the order of two options.

### Probe an opaque tool by hand first

Before writing a rule for a tool whose command line says little (the WinUI
XAML compiler, `makepri`, WPF's markup tasks), make it reproduce the reference
build's own output outside both build systems:

1. Take the parameters MSBuild passed from the log (task inputs are recorded).
2. Run the tool by hand on the reference tree, into a scratch directory.
3. Compare bytes with what MSBuild wrote. Identical is the baseline.
4. Change one thing: relative paths, another working directory, a parameter
   left out. Each run answers one question about what the rule must hold fixed.

That is how these were learnt before a line of Starlark existed: the XAML
compiler needs lists as `null`, not empty; it must run from the project
directory; it does not need the Visual C++ or SDK paths MSBuild gives it; its
two passes are independent actions; `makepri` names resources after their
staging path.

### The reference program defines "works"

Start the MSBuild-built program first, the way you will start Bazel's, and
write down what happens. Then the Bazel one.

- PowerToys' FancyZonesEditor, started alone, shows "Editor data parsing
  error." That is its behaviour without FancyZones, in both builds.
- Color Picker shows nothing at all until it is activated (Win+Shift+C, or a
  named event the runner signals). A run with no window is correct, and a demo
  needs the activation step.
- Hosts opens its window. Only this one could have been judged without the
  reference.

To judge a start mechanically: list the top-level windows the process owns
before and after (visible ones for "a window opened"; hidden ones with the
framework's window class for "the markup loaded"). For a program that waits to
be activated, find the event or message in the product's source and send it.

### A check that has never failed has not been tested

For each comparison, remove one thing on the Bazel side and see the check
report it: a define (diff no longer converges), a declared header (build
fails), an export from a `.def` (`check_outputs.py binaries`), a string from a
`.resw` (`pri`), an attribute in a `.xaml` (`xaml`). Restore the file from its
original bytes whatever happens. Two traps:

- A control can miss its target. "Remove the first `<data>` entry" from a
  `.resw` removed a sample inside the file's header comment and changed
  nothing.
- A control that re-runs `aquery` leaves that output behind. The next diff
  that reuses it reports the removed define again.

### Verify what you ship from only what you ship

An example overlay built in the clone it was made in and was missing ten
files: the emitters had copied them into that clone from the reference tree,
and the *migrated project's* `.gitignore` (`Generated Files/`, `x64/`) hid them
from the file listing the example was assembled from. Before calling an
overlay complete: delete every file a tool generated from the verification
clone, lay only the shipped files over it, build. Also search the shipped
files for the path of your user profile and for work files (`aquery.json`,
`diff.json`, lock files of the tools).

### Extending a reference build

Adding projects to a tree that is already built is an incremental build, and
its log is complete only for the new projects. For the others, C++ compiles
come back from the tracking logs and C# compiles from a design-time pass, but
the XAML, resource-index and copy steps did not run and are not in the log.
Extract the new log, take only the new targets (their model entries, report
entries, and what they add to `externals` and the path map) and keep the rest
from the extraction of the clean build. Rebuild the lock from the merged
report: selections of a package are numbered per lock, so BUILD files of
unrelated projects change labels and select the same files.

### Starting a product's programs on a developer machine

A program of a product writes settings and logs under the user's profile and
may hook the keyboard or the mouse. Read its start-up path before running it.
PowerToys' Color Picker takes the runner's process id as an argument: given
one, it installs no hook of its own and exits when that process does, so a
check script can pass its own. End such a program through its own terminate
event, not by killing it (Color Picker restores cursors on exit). Ask the owner
of the machine before starting the piece that loads everything (the runner).

When a script starts `bazel run` and captures its output, send it to a file.
Bazel first lists the files of a directory target (856 lines for one
program); a pipe nobody reads fills up and the program never starts.

## Symptom index

### Reference build, extraction, lock

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `ERROR: N source(s) in M project(s) have no recovered compile command` | the build never reached those projects, or the sources are the toolchain's own (`std.ixx`) | build the project; `--allow-incomplete` once only toolchain files remain | [C++](REFERENCE-msbuild.md#extracting-the-reference-model) |
| a C# target has sources and no options | its compile was up to date in the log | `--design-time` | [C#](REFERENCE-msbuild-csharp.md#compiler-command-lines-of-projects-the-build-skipped) |
| a XAML project is `excluded`, or has no `xaml` / `pri` in the report | its XAML steps did not run in this log | build it clean, or take it from the log that built it | [above](#extending-a-reference-build) |
| a NuGet download fails its integrity check | the lock had `contentHash`, which leaves the signature of a signed package out | the extractor takes `<id>.<version>.nupkg.sha512`; do the same by hand | [C++ field notes](REFERENCE-msbuild.md#field-notes) |
| the lock lists framework packs as `unresolved` | a pack is in no NuGet folder, so there was nothing to hash | `nuget_assets.py --fetch-packs` | [C#](REFERENCE-msbuild-csharp.md#nuget-lock-msbuilds-answer-do-not-re-resolve-it) |
| `C:/Users/<name>/.nuget/packages/...` in an emitted config or `exports_files` | a package used only through a source its consumers compile was not counted as used | the extractor records `sources` of a package; extract again | [C++](REFERENCE-msbuild.md#external-dependencies) |
| a vcpkg port fails to configure in the reference build | `MAX_PATH` under a long checkout path | shorter root | [case study §9](CASE-winget-cli-migration.md#9-what-the-frontend-got-wrong-until-these-targets-proved-it) |
| `generated_inputs` is shorter than what the compiles read | checkout under `%TEMP%` | build elsewhere | same |

### C++: compile and resources

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `error C4596: '<name>': illegal qualified name in member declaration` | `/std:c++latest` after `/permissive` turns conformance back on | the emitter states `/std:` first | [field notes](REFERENCE-msbuild.md#field-notes) |
| `fatal error C1083: Cannot open include file: 'pch.h'` in a generated source; or a shared source that compiles with another project's `pch.h` | without `/Yu`, `#include "pch.h"` is an ordinary include, looked up beside the source | `msvc_precompiled_header` (`pch.bzl`); do not add `/FI` | [precompiled headers](REFERENCE-msbuild.md#where-msbuild-differs-from-bazel) |
| `error RC1015: cannot open include file '../../../common/version/version.h'` (or the same from the compiler) | the path only resolves through an entry of the `INCLUDE` variable, which no command line shows | `include_environment` in the report; the emitter restates it | [field notes](REFERENCE-msbuild.md#field-notes) |
| a `.cpp` that another source `#include`s is compiled on its own | listed in `srcs` | `textual_hdrs` of a small `cc_library` | same |
| `C4005` macro redefinition (`NOMINMAX`) under `/WX` | rules_cc's default defines | the `/U` lines for `.bazelrc` | [rules_cc on Windows](BAZEL-RULES.md#rules_cc-on-windows-the-msvc-toolchains-fixed-points) |
| `undeclared inclusion(s) in rule ...` | a header reached through an include directory nobody declared | `headers` in the report (from the tracking log); a shared-items project becomes a header-only library | [C++](REFERENCE-msbuild.md#where-msbuild-differs-from-bazel) |
| warnings inside third-party headers fail the build | MSBuild marks those directories external through the environment | repeat `external_headers` flags | same |
| ambiguous symbols in WinRT or WRL headers | `/permissive-` missing | add it | [field notes](REFERENCE-msbuild.md#field-notes) |
| a binary without version information or icon | the `.rc` was never compiled | `windows_resources` | [resources](REFERENCE-msbuild.md#where-msbuild-differs-from-bazel) |

A compile error MSBuild does not have is a missing option, define or input.
Do not edit sources.

### C++: link

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `error LNK2019: unresolved external symbol GetFileVersionInfoSizeW` (a system function, its library is named by a `#pragma`) | a project library called `Version.lib` satisfies the request for the SDK's `version.lib`; the reference links because the import library was archived into a static library (`<Lib><AdditionalDependencies>`) | the report lists `deps` of a static library and the emitter hands them on | [linking](REFERENCE-msbuild.md#where-msbuild-differs-from-bazel), [replay](#replay-the-reference-before-forming-a-theory) |
| the link asks for `msvcrt.lib` in a `/MT` build | `/MT` given as a `copt` | `features = ["static_link_msvcrt"]` | [rules_cc on Windows](BAZEL-RULES.md#rules_cc-on-windows-the-msvc-toolchains-fixed-points) |
| a GUI program opens a console | `/SUBSYSTEM:` resolves last-wins and rules_cc adds `CONSOLE` | the project's flag in `linkopts` (it comes later) | [case study §9](CASE-winget-cli-migration.md#9-what-the-frontend-got-wrong-until-these-targets-proved-it) |
| `check_outputs.py`: "N functions are imported from another DLL than in the reference" | Bazel puts a target's static libraries before its `linkopts`; kernel32 and the API sets both export the function | none needed; same code at run time | [field notes](REFERENCE-msbuild.md#field-notes) |

### The diff

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| every native Bazel target is an aggregate without sources | compile actions use a parameter file `aquery` does not report | `--features=-compiler_param_file` | [what the emitted targets rely on](BAZEL-RULES.md#what-the-emitted-c-targets-rely-on) |
| `missing_target` for every program and DLL | the emitter names them after their files, the reference after projects | merge `emit_cc.any2bazel.json` (`target_map`) | [the native emitter](REFERENCE-msbuild.md#the-native-emitter) |
| `includes_diff` on package and generated directories | the same directory, spelled per build | the emitter's `include_map` entries | same |
| `missing_tu` for a source inside a NuGet package | the same file below the package in each build | the emitter's `source_dir_map` entry | same |
| `CONVERGED` with `test_binary_count` or `extra_target` warnings | a test DLL is not a test target to `aquery`: nothing was compared | `target_map` entry pairing it with the reference test | [tests](REFERENCE-msbuild.md#where-msbuild-differs-from-bazel) |
| `extra_cs_src` on `*_intermediate` targets | MSBuild compiles the intermediate XAML assembly from a shorter source list | accepted (warning) | [XAML](REFERENCE-msbuild-xaml.md#winui-3) |
| `defines_diff`: `NETSTANDARD*_OR_GREATER` | rules_dotnet defines them | accepted (warning) | [C#](REFERENCE-msbuild-csharp.md#rules_dotnet-what-it-reproduces-and-what-it-does-not) |
| not converged right after a negative control | the stored `aquery` output is the control's | query again | [above](#a-check-that-has-never-failed-has-not-been-tested) |
| converged, and the program is wrong | the difference is inside a generated file | `check_generated.py`, `check_outputs.py` | [C#](REFERENCE-msbuild-csharp.md#check_generatedpy-what-a-collapsed-directory-hides) |

### C# and rules_dotnet

The full table is in the
[C# guide](REFERENCE-msbuild-csharp.md#rules_dotnet-what-it-reproduces-and-what-it-does-not).
The ones with a recognisable message:

| Symptom | Cause | Fix |
|---|---|---|
| the build is clean and `ResourceManager` finds no resources at run time | resources are named `<assembly>.<path>`; MSBuild names them after the root namespace | `csc_assembly`, or the `named_resources` patch |
| `error CS9137: The 'interceptors' feature is not enabled` | `project_sdk = "web"` passes every analyzer of the ASP.NET pack | `csc_assembly` |
| a type "claims it is defined in WindowsBase, but it could not be found" | the base pack's facade next to the desktop pack's assembly | `overrides` in the lock |
| a GUI program opens a console | `winexe = True` still compiles `/target:exe` | `/target:winexe` in `compiler_options`, or the patch |
| an empty projection directory, no error | `cswinrt.exe` beyond `MAX_PATH` | short output name |
| one GUID differs in a projection | `midl` given the IDL path with forward slashes | backslashes |
| `.resources` differ by a few bytes | line ends in multi-line strings | the converter reads as MSBuild does |
| tests fail with "class not registered" | native WinRT components are not built yet | native first |
| a test cannot be started | runfiles path of 250 characters or more | shorter target names; open |

### WinUI 3: XAML and the resource index

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `Xaml Internal Error: Index was out of range`, or a `NullReferenceException`, in pass 1 | a list MSBuild did not pass was given as `[]` (`LocalAssembly`), or `ReferenceAssemblyPaths` as `null` | absent lists are `null`, except `ReferenceAssemblyPaths: []` | [WinUI 3](REFERENCE-msbuild-xaml.md#winui-3) |
| `error WMC9999: Could not find a part of the path` in pass 2 | the compiler resolves pages from its working directory | the launcher runs it from the project directory | same |
| the compiler's exit code and what it reported do not agree | its diagnostics are in the output file it writes, not in the exit code | the launcher prints them and fails on an error | same |
| `XamlTypeInfo.g.cs` differs: same types, another order | types are numbered in the order of the references | `reference_order` | same |
| the first line of generated code differs | `#pragma checksum` holds an absolute path | rewritten by the launcher, ignored by the check | same |
| a window opens to an exception; strings are missing | no `<Program>.pri` beside the program, or a library's compiled XAML is not in it | `winui_pri`; library `.xbf` under `<Library>\` | [resource index](REFERENCE-msbuild-xaml.md#the-resource-index) |
| `Visibility error` on a `program_directory` or metadata target | a program lays out another package's XAML outputs | the emitter makes those targets public | [field notes](REFERENCE-msbuild-xaml.md#field-notes) |
| "access denied" on the second run of a tool | it updates a copy of a read-only Bazel output | the launchers clear the attribute | same |

### WPF

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `BG1002: File '...\execroot\_main\App.xaml' cannot be found.` | the task takes the directory it was *created* in as the project's | change directory, then create the task | [WPF](REFERENCE-msbuild-xaml.md#wpf) |
| `UnauthorizedAccessException: Access to the path ...` in pass 2 | pass 2 updates the cache files pass 1 wrote | copy them writable into the action's directory | same |
| a target `<Project>_<random>_wpftmp`; a generator step missing from the real project | MSBuild compiles the intermediate assembly in a temporary project | excluded; its steps are taken for the real project | [report](REFERENCE-msbuild-xaml.md#what-the-report-records) |
| an image the markup shows is missing | a `Resource` item belongs in `<Assembly>.g.resources` | `wpf_resources` | [WPF](REFERENCE-msbuild-xaml.md#wpf) |
| generated code differs in `GeneratedCodeAttribute` or `#line` | SDK version of the toolchain; paths | ignored by the check | same |
| a project is copied an `Interop.*.dll` and noted | `<COMReference>`: generated from a type library registered on the machine | a snapshot until a rule exists | [what the emitter cannot generate](REFERENCE-msbuild-xaml.md#what-the-emitter-cannot-generate) |

### Starting a program

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| there is a `.dll` and nothing to start | a compile produces an assembly; the host executable is a separate step | `dotnet_apphost`, `program_directory` | [programs](REFERENCE-msbuild-xaml.md#programs) |
| `System.DllNotFoundException: Unable to load DLL 'Microsoft.WindowsAppRuntime.dll' or one of its dependencies` | several programs build into one directory; MSBuild's copy-if-newer target logs only the files that were not there yet (2 copies where the first program had 291) | the extractor completes the list (`copy` steps marked `up_to_date`); extract again | same |
| the program runs and shows nothing | possibly by design: it waits for a shortcut or an event | start the reference build's program the same way | [above](#the-reference-program-defines-works) |
| the program shows an error dialog | possibly the same without the rest of the product | same | same |
| a file under `not_laid_out` (`<Program>.dll.config`) | MSBuild generated it and nothing in the Bazel build provides it | open | [programs](REFERENCE-msbuild-xaml.md#programs) |
| a program lacks assemblies a library of it has | rules_dotnet lets one selection of a package replace another at run time | run-time files on programs only (the lock does this) | [C#](REFERENCE-msbuild-csharp.md#rules_dotnet-what-it-reproduces-and-what-it-does-not) |

## What still needs a person

| | State |
|---|---|
| files a project-specific step generates (a version header, a resource script made from translations, a merged manifest) | copied from the reference tree and listed as `snapshots` (10 in the slice); each wants a rule and a `--generated-map` entry |
| `<COMReference>` | interop assemblies copied; a rule would run `tlbimp` |
| CsWin32 run as a build task | not emitted |
| the product directory | each program has its own; the tree the runner loads modules from is not assembled, and the runner has not been started |
| managed tests on long paths | 42 of 58 PowerToys suites cannot be started |
| what should be shared code | [FUTURE-windows-bazel-modules.md](FUTURE-windows-bazel-modules.md) |

## Shell and machine traps

| Trap | What to do |
|---|---|
| Git Bash turns `//pkg:target` and `/option` arguments into paths | `MSYS_NO_PATHCONV=1` (and `MSYS2_ARG_CONV_EXCL="*"`). With it set, `/c/...` paths given to native programs are no longer converted either: pass Windows paths |
| Git Bash heredocs lose backslashes, quoted or not (`\\n` became a line break, `\\module` lost one) | write scripts and patches as files; keep regular expressions and Windows paths out of heredocs |
| PowerShell's `>` writes UTF-16 | redirect in Git Bash, or expect only `aquery.json` to be read either way |
| Bazel picks the newest Visual Studio | pin `BAZEL_VC`, `BAZEL_VC_FULL_VERSION`, `BAZEL_WINSDK_FULL_VERSION` to what MSBuild used |
| a file copied from a Bazel output is read-only | clear the attribute before a tool updates it |
| `MAX_PATH`: `cswinrt.exe` writes nothing and says nothing; Bazel's test wrapper does not start a program near 250 characters; vcpkg fails under a long root | short output names, short roots |
| the output base grows by gigabytes per program | `startup --windows_enable_symlinks` |
| a captured `bazel run` never starts the program | its output went to a pipe nobody read |
| an include directory with a space (`Generated Files`) | through `copts`, not `includes` |
