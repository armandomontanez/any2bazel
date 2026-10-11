# Case study: winget-cli and PowerToys → Bazel, and what an MSBuild build does not say

**Subjects:** [microsoft/winget-cli](https://github.com/microsoft/winget-cli)
(`AppInstallerCLI.sln`, Release|x64: 41 targets, 527 C/C++ compiles, C++/WinRT,
NuGet and vcpkg) and one native test project of
[microsoft/PowerToys](https://github.com/microsoft/PowerToys)
(`UnitTests-CommonUtils`, a 36-source CppUnitTest DLL). Both built with MSBuild
18.10, toolset v145 (MSVC 14.51.36231), Windows SDK 10.0.26100.0; Bazel 9.2.0
with rules_cc 0.2.17 on the same compiler.

A second pass took the managed code of both repositories through
`rules_dotnet` 0.22.2 (.NET SDK 10.0.401 in the reference builds, 10.0.400
under Bazel): all 22 C# compiles of winget-cli and the 199 of PowerToys that
involve no XAML. That part is section 10.

A third pass took a 33-project slice of PowerToys all the way to programs that
start: 17 native projects through an emitter (the runner, module DLLs,
C++/WinRT components), WinUI 3 and WPF XAML, resource indexes, and the
directory each program runs from. That part is section 11.

**Outcome:** on the native side this is a **slice**, not a migration. Three
targets are built by Bazel from trees that contain no MSBuild output:

| Target | What it exercises | Parity diff | Beyond the diff |
|---|---|---|---|
| winget `AppInstallerSharedLib.lib` (30 TUs) | NuGet, vcpkg, the C++/WinRT projection, a shared-items project | converged, 0 errors, 0 warnings | projection headers byte-identical to MSBuild's (1,358 files) |
| winget `winrtact.dll` (6 TUs) | MIDL, `.def`, embedded manifest, Control Flow Guard, Spectre runtime | converged, 0 errors, 0 warnings | same exports, subsystem, DLL characteristics and manifest as MSBuild's DLL; same five Spectre runtime libraries read by the link |
| PowerToys `Common.Utils.UnitTests.dll` (36 TUs) | static CRT, `/std:c++23preview`, resources, CppUnitTest, vstest | converged, 0 errors, 0 warnings | `bazel test` runs the same 484 tests with the same results as the MSBuild-built DLL |

Twenty-four negative controls — take one flag, define, include directory,
source, link option or library away from the Bazel side — each break
convergence. A twenty-fifth (removing a library from the *test* DLL's link) does
not: test binaries are compared at compile level only, as for every frontend.

The workspace overlays are in [`examples/winget-cli/`](../examples/winget-cli/)
and [`examples/powertoys/`](../examples/powertoys/).

This document is about what the slice **found**. An MSBuild build is a set of
project files plus the machine they ran on plus the order they ran in; the
second and third are not written down anywhere. Expressing three targets in a
system that requires inputs to be declared was enough to hit every item below.
§1–§6 are properties of the builds. §7–§8 are about tests. §9 is about the
tool: places where the frontend was wrong until one of these targets proved it.

---

## 1. Dependencies that exist only as build order

`winrtact.dll` compiles `src/WinGetServer/WinGetServer_c.c`. That file is not in
the repository. `WinGetServer.vcxproj` produces it: its MIDL step runs in the
project directory, so `WinGetServer.h`, `WinGetServer_c.c` and
`WinGetServer_s.c` land **in the source tree**, and three other projects list
them by relative path:

```
undeclared_generated_inputs:
  Microsoft.Management.Configuration.OutOfProc  [src/WinGetServer]
  Microsoft.Management.Deployment.OutOfProc     [src/WinGetServer]
  UndockedRegFreeWinRT                          [src/WinGetServer]
```

None of the three has a `ProjectReference` to `WinGetServer`. The solution
builds them in an order that works.

PowerToys has the same shape, and there it is easy to demonstrate.
`UnitTests-CommonUtils` includes `src/common/version/version.h`, which includes
`"Generated Files\version_gen.h"` — a header `version.vcxproj` writes beside
itself from `src/Version.props`. The test project does not reference
`version.vcxproj`. Built on its own from a clean checkout:

```
src\common\version\version.h(6,10): error C1083: Cannot open include file:
    'Generated Files\version_gen.h': No such file or directory
```

Build `version.vcxproj` first and it succeeds.

The extractor now reports this (`generated_inputs` per target, and
`undeclared_generated_inputs` at the top of the report), from the compiler's
read tracking log: every generated file a project read, which project's
directory it sits in, and whether that project is referenced. In Bazel the
generator is a target (`bazel/midl.bzl`, a `write_file`), its outputs are
labels, and the consumer names them.

There is a second half to this. With MSBuild's copy of `version_gen.h` still in
the source tree, the first Bazel build of the PowerToys DLL failed:

```
undeclared inclusion(s) in rule '//src/common/UnitTests-CommonUtils:Common.Utils.UnitTests.dll':
  'src/common/version/Generated Files/version_gen.h'
```

`version.h` includes the file relative to itself, so the compiler found
MSBuild's leftover before Bazel's generated one. Bazel refused it. That is the
reason the rule in the reference guide is *build in a tree without MSBuild
outputs*: a generated file left in the source tree is indistinguishable from a
source file to everything except the include check.

## 2. An up-to-date check that looks at a stamp, not at the output

The two winget binary logs already on this machine both ended in

```
error C1083: Cannot open include file: 'winrt/Windows.Foundation.h'
```

and so did a fresh incremental build.
`Microsoft.Management.Deployment\x64\Release\Generated Files` was empty. The
CppWinRT targets use the tool's response files (`*.vcxproj.cppwinrt_*.rsp`,
`*.mdmerge.rsp`, `*.midlrt.rsp`) as their up-to-date markers. The response
files were there, so the steps were skipped, and nothing checks that the
headers they would have produced exist. Deleting the four response files in
that directory fixed the build.

In Bazel the projection is an action whose output is the directory; it cannot
be "up to date" and absent.

## 3. The same 1,358 headers, generated once per project

Importing the CppWinRT NuGet package makes a project run `cppwinrt.exe` over
every contract `.winmd` of the Windows SDK into its own
`<IntDir>Generated Files`. Fourteen winget projects do. The command line is
identical apart from the output directory, and the output depends only on the
tool version and the SDK version.

`bazel/cppwinrt.bzl` runs it once (`//:winrt_platform`). Its output was compared
with MSBuild's for `AppInstallerSharedLib`: 1,358 files, byte for byte.

For the diff this means several reference include directories correspond to one
Bazel directory, which `include_map` expresses directly: each project's
`x64/Release/Generated Files` and `bazel-out/.../winrt_platform` map to the same
token.

## 4. Every binary is offered every library

vcpkg's MSBuild integration appends `<installed>\lib\*.lib` to every link line.
The link of `winrtact.dll` opened all thirteen vcpkg libraries of the tree
(brotli, c-ares, Catch2, curl, …). It uses one, `detours.lib`.

A wildcard names nothing, so the extractor reports those libraries as
`libraries_offered_by_wildcard` and does not turn them into dependencies. On the
Bazel side `bazel/vcpkg.bzl` generates one target per port from vcpkg's own file
lists and the DLL depends on `@vcpkg//:detours`. The Bazel link read exactly one
vcpkg library.

The same rule is why the vcpkg recipe runs the project's manifest, baseline,
overlay triplets and overlay ports unmodified instead of taking the libraries
from somewhere convenient: an earlier attempt at this migration re-sourced
jsoncpp and yaml-cpp from nuget.org, without a hash, in versions the project
does not pin.

## 5. The C runtime is chosen by a search path

Both winget targets set `<SpectreMitigation>Spectre</SpectreMitigation>`. On the
compile line that is `/Qspectre`. On the link line it is only

```
/LIBPATH:...\VC\Tools\MSVC\14.51.36231\lib\spectre\x64
```

and the libraries that come from there — `msvcrt.lib`, `msvcprt.lib`,
`vcruntime.lib`, `oldnames.lib`, `comsuppw.lib` — are named by `/DEFAULTLIB`
directives inside object files, not on any command line. A Bazel build that
mirrors every flag of the reference and omits the search path links the
unmitigated runtime, and nothing fails.

`bazel/msvc_toolset.bzl` exposes the directory of the pinned toolset as
`@msvc//:spectre_libs` and refuses to build if the component is not installed.
The differ keeps this one search path as a link fact
(`/LIBPATH:@msvc/lib/spectre`). `link /VERBOSE:LIB` on the Bazel build and the
reference link's read log list the same five libraries from that directory.

## 6. Include directories that do not exist

| Project | On the command line | On disk |
|---|---|---|
| winget `AppInstallerSharedLib` | `$(ProjectDir)Telemetry` | the directory is `Public\Telemetry` |
| winget `AppInstallerCLI`, `WinGetUtil`, `WindowsPackageManager` | `src\binver\include` | no such directory |
| PowerToys `UnitTests-CommonUtils` | `$(VCInstallDir)UnitTest\include`, and `/LIBPATH:$(VCInstallDir)UnitTest\lib` | the framework has lived in `VC\Auxiliary\VS\UnitTest` since Visual Studio 2017 |

The compiler skips a missing include directory silently. The PowerToys test
project compiles only because Visual Studio also puts the real location on
every project's default include and library path.

The report lists these as `dead_include_dirs`. They need no Bazel counterpart;
`ignore.include_prefixes` records the decision.

---

## 7. A test project is not a test

MSBuild builds test projects. Nothing in either repository's MSBuild graph runs
them.

`UnitTests-CommonUtils` builds a **DLL**. It has no `main`; it exports test
metadata, and `vstest.console.exe` — part of Visual Studio, found by CI — loads
it through an adapter. There is nothing for `cc_test` to execute. The Bazel side
is a `cc_binary(linkshared = True)` plus `vstest_test` (`bazel/vstest.bzl`),
which runs the DLL with the runner of the *pinned* Visual Studio instance and
writes a TRX report to the test's outputs.

| | Discovered | Passed | Failed |
|---|---|---|---|
| MSBuild-built DLL, `vstest.console.exe` | 484 | 481 | 3 |
| Bazel-built DLL, `bazel test` | 484 | 480 | 4 |

The three common failures are named-pipe security tests that depend on the
account running them. The fourth, `ThreadSafety_ConcurrentAccessAndRead_NoDataRace`,
was then run six times against each DLL alone: it failed twice against each.
It is flaky in the source, not different between the builds. With every MSBuild
output moved out of the tree and after `bazel clean`, the DLL rebuilt from
nothing and the run was the same (that test filtered out: 483 run, 480 passed,
the same 3 failed).

winget's native tests are the other common shape: `AppInstallerCLITests.exe` is
a Catch2 executable with its own `main`. Its project copies **268 data files**
from `TestData\` flat into the output directory next to the executable, and the
tests find them through the executable's path. In Bazel those are `data`, and
the lookup becomes a runfiles path or an argument. The report records each copy
under `steps`. That binary was not built here (it needs five libraries outside
the slice), so this part is described, not verified.

## 8. Warnings in other people's headers

The PowerToys DLL compiles with `/W4 /WX`. Its first Bazel build failed inside
spdlog:

```
spdlog/common.h(32): error C2220: the following warning is treated as an error
spdlog/common.h(32): warning C4005: 'SPDLOG_HEADER_ONLY': macro redefinition
```

MSBuild passes `/external:anglebrackets /external:W0` and sets `EXTERNAL_INCLUDE`
(from the `ExternalIncludePath` property) to the vcpkg and NuGet directories.
Headers reached that way produce no warnings. The environment variable is not
on the command line, and the two flags are diagnostics, which the differ
ignores on purpose.

So a set of options that changes nothing about the output decides whether the
build passes. The report now carries them (`external_headers`: the flags and
the directories), and the Bazel target repeats the flags in `copts`.

---

## 9. What the frontend got wrong until these targets proved it

**An incremental build logs almost nothing.** Of the 549 compile actions in the
winget model (527 C/C++, 22 C#), 169 have a command line in the binary log. The
rest were up to date, and MSBuild logs no command for a compile it skips. They
come from the tracking logs MSBuild itself checks (366) and from skipped C#
targets' declared inputs (14). An earlier attempt that read only the log ended
up with 8 targets in its model. The extractor now accounts for every evaluated
source and fails if one is unrecovered.

**"Converged" with nothing compared.** The first PowerToys diff said
`CONVERGED errors=0 warnings=2`. Reference test targets were only compared with
Bazel *test* targets, and a `cc_binary` DLL is not one to `aquery`; the 36
sources had been matched by name and their flags never looked at. The two
warnings said so, quietly. A `target_map` entry that pairs a Bazel target with a
reference test now gives it the test role. With that, the same diff reported 30
real include differences (§6), and seven controls on that target fire.

**A library that is merely an input is not linked.** Bazel-side dependencies
were inferred from every `.lib` among a link action's inputs. Declaring the
Spectre directory made its 44 libraries inputs, so the diff believed
`winrtact.dll` linked all of them, and removing `comsuppw.lib` from the command
line would have gone unnoticed. For MSVC links only the command line counts
now, and that control fires.

**Link options resolve last-wins, like compile options.** rules_cc puts
`/SUBSYSTEM:CONSOLE` on every link. Comparing flags as a set, a project's
`/SUBSYSTEM:WINDOWS` matched whether it came after the default (a GUI image) or
before it (not one). Checked with `dumpbin`: the last one wins, for
`/SUBSYSTEM`, `/MANIFEST`, `/DYNAMICBASE` and `/guard`. The same experiment
showed that `/DYNAMICBASE` and `/NXCOMPAT` are what the linker does unasked,
and that `/MANIFEST` is **not**: a bare `link.exe` writes no manifest.

**rules_cc's defaults cannot be turned off, and are not MSBuild's.** The MSVC
toolchain defines `NOMINMAX`, `_CRT_SECURE_NO_DEPRECATE`,
`_CRT_SECURE_NO_WARNINGS` and `_WIN32_WINNT=0x0601` on every compile;
`--features=-default_compile_flags` fails analysis. MSBuild defines none of
them. A winget source that defines `NOMINMAX` itself is then a C4005
redefinition, an error under `/WX`. The recipe is four lines of `.bazelrc`
(`/U...`, and `BAZEL_WIN32_WINNT=/U_WIN32_WINNT`), after which the effective
defines on the two sides are equal.

**Paths.** Tracking logs upper-case every path. Restoring the case from disk
failed for paths outside the project directory (`/I..\UTILS`) and for
directories that do not exist, so config rules for include paths now match
case-insensitively on Windows builds.

Two environment limits, found the hard way and worth knowing before choosing
where to put a checkout:

- vcpkg run from MSBuild builds under `<repo>\vcpkg_installed\...\blds`. From a
  checkout whose root path was 141 characters long, CMake's compiler check
  overflowed `MAX_PATH` and the port failed to configure. (Bazel's copy builds
  under the output base, which is short.)
- The file tracker does not record reads under `%TEMP%`. A checkout there has
  complete command tracking logs but incomplete read logs, so
  `generated_inputs` under-reports.

---

## 10. C#: the reference is the compiler's command line, and rules_dotnet has opinions about it

The second pass took the managed side of both repositories: every C# project of
winget-cli (22 compiles) and every C# project of PowerToys that does not compile
XAML or depend on one that does (199 compiles in the reference build), through
`rules_dotnet` 0.22.2, with the WinRT interop chain they hang off. The method is
in [REFERENCE-msbuild-csharp.md](REFERENCE-msbuild-csharp.md); this section is
what it found.

**A project file does not say what the compiler was given.** winget's
`Microsoft.Management.Configuration.UnitTests` references the CsWinRT package;
its compile gets one of that package's two assemblies, because a `.targets`
file of the repository removes the other for projects that embed the runtime.
`Microsoft.Win32.Registry` is in thirteen compiles' restore graphs and on none of
their command lines: the framework already has it (61 packages are displaced
that way somewhere in winget). 102 PowerToys projects that use WPF or WinForms
lose up to three assemblies of the base reference pack (`WindowsBase.dll`,
`System.Drawing.dll`, `Microsoft.VisualBasic.dll`) to the desktop pack. None of that is
derivable from a `.csproj`; all of it is in the `Csc` task's arguments. So the
NuGet lock is built from those arguments, and each target's selection from a
package is its own.

**rules_dotnet decides part of the command line, and five times that part was
wrong for a project.** In the order they were hit:

1. *Every* assembly of the .NET Framework reference pack is passed (339
   references where MSBuild passes 221). Two of the extra ones declare a
   `Microsoft.Windows` namespace, and in winget's PowerShell engine
   `Windows.Foundation`, written inside `namespace Microsoft.WinGet...`, stopped
   meaning the Windows one.
2. `project_sdk = "web"` passes every analyzer of the ASP.NET pack, including a
   source generator MSBuild runs only on request; the compile fails.
3. An embedded resource is always named `<assembly>.<path>`. PowerToys names
   its assemblies `PowerToys.X` and its namespaces `X`; MSBuild names the
   resource after the namespace, `ResourceManager` asks for that name, and
   sixteen projects would have compiled and found none of their resources at
   run time.
4. The base pack's facade `WindowsBase.dll` is passed next to the desktop
   pack's real one; `DependencyObject` "claims it is defined in WindowsBase,
   but it could not be found".
5. `winexe = True` is accepted and the compile still says `/target:exe`:
   eleven GUI programs would have opened a console.

The first three are closed by one template, `csc_assembly.bzl`: a compile with
exactly the reference build's references, analyzers, resource names and
options, on rules_dotnet's own toolchain, whose result goes back into
rules_dotnet through `import_library`. 3 of winget's 22 targets and 16 of
PowerToys' 167 use it. The fourth is closed in the lock (a reference that
displaces a framework assembly becomes a dependency named like the assembly,
which is the one case in which rules_dotnet drops its own copy), the fifth by
stating `/target:winexe` again in `compiler_options`. Nothing in rules_dotnet
is patched. Items 3 and 5 compile and link cleanly; only the comparison with
the reference command line shows them.

**A converged diff was wrong three times, and each time the generated files
said so.** The diff compares command lines, and a command line names a
generated directory, not what is in it:

- winget prefixes every generated C#/WinRT file with `#pragma warning disable`
  and compiles the copies, *except* `Module.cs`, which carries an unversioned
  `SupportedOSPlatform` attribute. The Bazel rule copied all of them. The diff
  converged; the platform analyzer then rejected every versioned WinRT call.
- One GUID in one projection was different. `midl`, given the IDL as
  `a/b/X.idl` instead of `a\b\X.idl`, synthesizes a different IID for one
  default interface of the configuration API. Same file, same options, same
  93 references. The assembly compiled. It could not have activated a
  component built by MSBuild from the same source.
- One PowerToys projection directory was empty: `cswinrt.exe` writes nothing,
  and reports nothing, when the output path exceeds `MAX_PATH`, and
  `<package>/<target>_cswinrt.cs/` repeats a 61-character project name twice.
  The compile succeeded because that project uses none of it.

`check_generated.py` exists because of the first, found the other two, and
then found that seven `.resources` files differed by three bytes each (MSBuild
reads `.resx` with a reader that does not normalize line ends).

**The design-time pass is trustworthy because it is checked.** 14 of winget's
22 compiles were skipped as up to date in the log that was used first. The
compile-free second pass recovered all 14 command lines, and for every project
that *did* compile the two were compared: 22 of 22 equal on winget, 198 of 199
on PowerToys. The 199th is a test project that links in a WPF window from
another project, so the real build runs the markup compiler and the design-time
one does not; it is reported, and it is how that project was recognised as XAML
at all (its own directory has no `.xaml`; it links one in).

**A machine supplies more than the project restores.** The WindowsDesktop and
ASP.NET reference packs come from `dotnet\packs`, the .NET Framework reference
assemblies from the Visual Studio targeting pack even though the NuGet package
is restored beside them. Each is a copy of a package, so each is locked as that
package, downloaded by hash, and compared with what the build read: 651 files
across five packs, all identical. PowerToys' generated analyzer configs carry
absolute paths into the NuGet cache (the metadata CsWin32 is pointed at) for 17
projects; restated verbatim, the Bazel build would have read the cache of
whoever ran the reference.

### Result

| | winget-cli | PowerToys |
|---|---|---|
| C# compiles in the reference build | 22 | 199 (non-XAML scope) |
| Built by Bazel from a tree without MSBuild output | 22 | 167 |
| ... of them through `csc_assembly` | 3 | 16 |
| `diff.py` | converged, 0 errors | converged, 0 errors |
| `check_generated.py` | 54 trees and resource sets, 426 files, identical | 750 trees and resource sets, 867 files, identical |
| Excluded: compiles XAML | 0 | 1 (found from the log) |
| Not emitted | 0 | 31 |

The 31 are four projects and their 27 dependents: two reference COM type
libraries (`<COMReference>`; MSBuild generates the interop assemblies from what
is registered on the machine), two run CsWin32 as a build task that writes a
source file, and the rest are tests of those (24 of them UI tests on one
automation library). Each is listed with its reason in `emit_dotnet.summary.json`.

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

---

## 11. Programs: what has to be true before a window opens

The third pass chose its scope by what a person can start: PowerToys' Hosts
editor and Color Picker (WinUI 3), the FancyZones editor and the Workspaces
launcher (WPF), the runner and a native utility, and the closure of what they
reference. 33 projects of 353. BUILD files came from two emitters
(`emit_cc.py`, `emit_dotnet.py`); the guides are
[REFERENCE-msbuild.md](REFERENCE-msbuild.md#the-native-emitter) and
[REFERENCE-msbuild-xaml.md](REFERENCE-msbuild-xaml.md), and the failures are
indexed by message in the
[playbook](REFERENCE-msbuild-playbook.md#symptom-index). What follows is the
part no log states.

**The environment is an input, and it leaks.** `rc` and the compiler search
the `INCLUDE` variable after their `/I` directories. PowerToys' variable has
an entry that is the *name of a property* (`PreventSdkUapPropsAssignment`),
a relative path that exists nowhere. Sources include
`../../../common/version/version.h` from a directory three levels deep, and
it resolves only because that entry gives the path one more level to climb.
No command line shows it; the report now lists such entries.

**A library can hide a system library, and another can carry it.**
`#pragma comment(lib, "version.lib")` asks for the SDK's library. PowerToys
has a project that produces `Version.lib`; with it on the link line the linker
considers the request met. The reference links anyway, because a third
project's `<Lib><AdditionalDependencies>` had the librarian copy the SDK's
import library into a static library. Three wrong theories preceded that
finding; replaying the reference link and exchanging one library at a time is
what found it.

**Compiling without the precompiled header is another build.** With `/Yu` the
compiler never opens `pch.h`. Without it, `#include "pch.h"` is looked up
beside the source: a source one project borrows from another gets the other
project's header, and a generated source in an output directory gets none.
rules_cc has no `/Yc`; the emitted targets use a rule for it.

**The XAML compiler's input has rules nobody wrote down.** It is the same
compiler as a task and as a program, and the program writes what the task
writes, byte for byte, once three things hold: a list MSBuild did not pass is
`null` (empty, pass 1 indexes into it), the working directory is the
project's, and references come in MSBuild's order (the type table is numbered
as types are met). Each was found by running the program by hand against the
reference build's own files before any rule existed.

**A WinUI program does not run without a file no project lists.** Strings,
images and the compiled markup itself are found through `<Program>.pri`.
MSBuild builds one at the end of every project with resources, 46 of them in
PowerToys without a line of XAML.

**WPF's compiler is a library, and MSBuild builds a project you did not
write.** The markup compiler exists only as task classes; they are hosted in a
small program. For the pass in between, MSBuild generates a temporary project
(`<Project>_<random>_wpftmp`) and runs some of the real project's generators
there, so the real project's log says they were up to date.

**The second program into a directory logs two copies.** PowerToys builds
every WinUI program into `WinUI3Apps`. MSBuild's copy-if-newer target declares
inputs and outputs, so its Copy task is handed only what is not there yet: 291
files for the first program, 2 for the second. The second program, built by
Bazel from that log, ended at startup for want of
`Microsoft.WindowsAppRuntime.dll`. The complete list is in the log as the
items a target computed, and the extractor now reads it. A clean build of the
whole product would have had the same hole for every program but the first.

**"It works" is what the reference program does.** The FancyZones editor,
started alone, shows an error dialog in both builds. Color Picker shows
nothing in either until its shortcut is pressed. Only the Hosts editor simply
opens a window.

**An example can build only where it was made.** The overlay shipped without
ten generated files it needs: the emitters had copied them into the clone it
was verified in, and PowerToys' own `.gitignore` kept them out of the listing
the overlay was assembled from. Verification now starts by deleting them.

### Result

| | |
|---|---|
| Emitted and built from a tree without MSBuild output | 17 of 17 C++ projects, 16 of 16 C# projects (two temporary WPF projects excluded) |
| `diff.py` | converged, 0 errors, 20 warnings (defines rules_dotnet adds; sources of the intermediate XAML assemblies) |
| `check_outputs.py` | XAML compiler output of 6 projects, 4 resource indexes, exports, imports and resources of 8 binaries: same |
| Programs | Hosts and the Workspaces launcher open their windows; Color Picker shows its picker when activated; the FancyZones editor shows the reference's dialog |
| Negative controls | 5 of 5 reported |
| Copied from the reference tree | 10 files of project-specific generators, and two COM interop assemblies |

The overlay is [`examples/powertoys/workspace-ui/`](../examples/powertoys/workspace-ui/),
described in [README-ui.md](../examples/powertoys/README-ui.md). Which of the
templates it needed should be shared code is in
[FUTURE-windows-bazel-modules.md](FUTURE-windows-bazel-modules.md).

---

## What is not done

- **Native code beyond the slices**: 15 of winget's 17 native targets and 93 of
  PowerToys' 110 native projects. They are extracted and reported.
- **The product.** PowerToys' runner is built and has not been started: the
  directory it loads modules from is not assembled.
- **C#: the other 20 WinUI projects of PowerToys** (Settings among them),
  **packaging, and 31 PowerToys projects** (section 10): four that reference
  COM type libraries or run CsWin32 as a build task, and their dependents.
  MSIX and WiX projects are extracted and reported.
- **Most managed tests under `bazel test`** (section 10): 12 of 58 PowerToys
  suites match the reference, 4 need native components, 42 cannot be started
  on Windows for the length of their runfiles path; winget's are VSTest-hosted
  and need native code.
- **`AppInstallerCLITests` under `bazel test`** (§7).
- **Link parity of test binaries.** Compared at compile level only.
- **Build type.** `/O2 /Oi /GL` and `/LTCG` are not mirrored. The two
  `winrtact.dll` builds differ in one imported API set
  (`api-ms-win-crt-math`).
- **Precompiled headers in the first pass's three targets.** rules_cc has none
  for MSVC and those targets compile without one; the emitted targets of
  section 11 use `pch.bzl`.
- **Hermeticity.** The compiler, SDK, vcpkg and vstest are the host's, pinned by
  version and checked, not downloaded. vcpkg runs in a repository rule.
- **`version_gen.h` restates `src/Version.props`** in a BUILD file. MSBuild also
  folds `BUILD_SOURCEVERSION` / `GITHUB_SHA` from the environment into it; the
  Bazel equivalent is stamping.
- **Linux.** Extraction needs MSBuild and the build tree. The differ and its
  tests run anywhere; they were run on Windows and under a POSIX-path
  emulation, not on a Linux machine.

## Appendix: reproducing

```bash
# Reference (from the repo root; a full build, or an incremental one after it)
msbuild src\AppInstallerCLI.sln -restore -p:RestorePackagesConfig=true \
    -p:Configuration=Release -p:Platform=x64 -m -bl:msbuild.binlog
python3 scripts/extract_msbuild.py msbuild.binlog . model.msbuild.json --report msbuild.report.json

# Bazel side, in a checkout WITHOUT MSBuild outputs, overlay copied in
bazel build //src/AppInstallerSharedLib \
    //src/Xlang/UndockedRegFreeWinRT/src/UndockedRegFreeWinRT/UndockedRegFreeWinRT:winrtact.dll
bazel aquery 'mnemonic("CppCompile|CppLink|CppArchive", //...)' \
    --features=-compiler_param_file --include_param_files --output=jsonproto > aquery.json
python3 scripts/extract_bazel.py aquery.json . model.bazel.json
python3 scripts/diff.py model.msbuild.json model.bazel.json any2bazel.json > diff.json
python3 scripts/triage.py diff.json
```

The findings in §1, §3–§6 and §8 can be read straight out of
`msbuild.report.json`: `undeclared_generated_inputs`, each target's `steps`,
`libraries_offered_by_wildcard`, `link_flags`, `dead_include_dirs` and
`external_headers`.
