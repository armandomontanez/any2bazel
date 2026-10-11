# PowerToys → Bazel: one native test project, run under `bazel test`

A Bazel workspace overlay for one project of
[PowerToys](https://github.com/microsoft/PowerToys):
`src/common/UnitTests-CommonUtils`, a Microsoft C++ unit-test (CppUnitTest) DLL
with 36 sources. It exists to show the part of an MSBuild migration that has no
CMake counterpart: **a test that is a DLL**, loaded by a runner MSBuild never
invokes. The narrative is in
[`docs/CASE-winget-cli-migration.md`](../../docs/CASE-winget-cli-migration.md)
(§1 and §6–§8).

**This is a slice.** One project out of several hundred.

A second overlay, [`workspace-ui/`](workspace-ui/), takes 31 projects through
the emitters: the runner, module DLLs, two WinUI 3 apps and a WPF app that start
under Bazel. It is described in [README-ui.md](README-ui.md).

**What it achieves:**

- `Common.Utils.UnitTests.dll` builds from a tree with every MSBuild output
  moved away (`packages\`, `vcpkg_installed\`, `x64\`, the intermediate
  directories, and the header `version.vcxproj` generates into the source tree).
- `diff.py` reports `CONVERGED errors=0 warnings=0` against the MSBuild
  Release|x64 reference (36 translation units, `include_tests` on). Seven
  negative controls on the compile side each break convergence. The link line
  of a test binary is not compared (true of every frontend).
- `bazel test //src/common/UnitTests-CommonUtils:unit_tests` discovers and runs
  the same 484 tests as `vstest.console.exe` on the MSBuild-built DLL, with the
  same results: three named-pipe security tests fail on this machine under both
  builds, and one test is flaky under both (2 failures in 6 runs each). With
  those four filtered out, the Bazel test passes, 480 of 480.

## Layout

`workspace/` mirrors the paths these files occupy inside a PowerToys checkout.
`windows_sdk.bzl`, `msvc_toolset.bzl`, `vcpkg.bzl` and `cppwinrt.bzl` are the
same files as in [`examples/winget-cli/`](../winget-cli/).

| Path | What it is |
|------|-----------|
| `bazelrc.txt` | → `.bazelrc`. Toolchain pin and the rules_cc default-define undo. **`BAZEL_VC` is a machine path** |
| `MODULE.bazel` | `rules_cc`, `bazel_skylib`; WIL and CppWinRT from the feed in `nuget.config`, with NuGet's own SHA-512; the repository rules |
| `bazel/rc.bzl` | `windows_resources`: a `.rc` script to a `.res` that is linked by depending on it. rules_cc has no resource compiler; without this the DLL has no version information |
| `bazel/vstest.bzl` | `vstest_test`: runs a CppUnitTest DLL with `vstest.console.exe` of the **pinned** Visual Studio instance and writes a TRX report to the test outputs |
| `bazel/msvc_toolset.bzl` | Also provides `@msvc//:cppunittest` (headers and library path of the test framework) and the runner's location |
| `src/common/version/BUILD.bazel` | The generated version header as a target. MSBuild writes it into the source tree and the test project consumes it without referencing its producer |
| `src/common/BUILD.bazel` | The headers other projects reach through include directories; sources that consumers compile themselves |
| `src/common/UnitTests-CommonUtils/BUILD.bazel` | The DLL, its resources and the test target |
| `any2bazel.json` | Scope and alignments. `target_map` is what tells the differ this `cc_binary` is the reference's test |

## Reproducing

Needs Windows, Bazel 9.2.0, and Visual Studio with MSVC 14.51.36231, the C++
unit-test framework, the vcpkg component and Windows SDK 10.0.26100.0.

```bash
git clone https://github.com/microsoft/PowerToys && cd PowerToys
cp -r .../examples/powertoys/workspace/. . && mv bazelrc.txt .bazelrc   # edit BAZEL_VC
bazel test //src/common/UnitTests-CommonUtils:unit_tests --test_output=errors
```

The reference for the diff is this project built alone. That takes four steps,
because of what the project leaves unsaid (see below); `C` stands for
`-p:Configuration=Release -p:Platform=x64 -p:SolutionDir=%CD%\`:

```bat
msbuild src\common\UnitTests-CommonUtils\UnitTests-CommonUtils.vcxproj -t:Restore -p:RestorePackagesConfig=true C
msbuild src\common\COMUtils\COMUtils.vcxproj -t:Restore -p:RestorePackagesConfig=true C
msbuild src\common\version\version.vcxproj C
msbuild src\common\UnitTests-CommonUtils\UnitTests-CommonUtils.vcxproj C -bl:msbuild.binlog
```

The second line restores WIL, which the test project uses but does not list.
The third generates `version_gen.h`. Keep the checkout path short (vcpkg builds
inside it and overflows `MAX_PATH`) and outside `%TEMP%` (the file tracker does
not record reads there).

Do the Bazel build in a **different** checkout, or delete
`src\common\version\Generated Files` first. Otherwise the compiler finds
MSBuild's copy of `version_gen.h` beside `version.h` and Bazel rejects the
compile as an undeclared inclusion — correctly.

## What the project needed that its `.vcxproj` does not say

- `version.vcxproj` built first: the test project includes a header it
  generates and has no reference to it.
- `Microsoft.Windows.ImplementationLibrary` restored by *some other* project:
  the test project's include path names the package directory, but its own
  `packages.config` lists only CppWinRT.
- `/external:anglebrackets /external:W0` and an `EXTERNAL_INCLUDE` environment
  variable: with `/W4 /WX`, spdlog's headers do not compile without them.
- The unit-test framework from Visual Studio's default include and library
  paths: the directories the project names for it do not exist.
- `vstest.console.exe` to run anything.

## The C# side: a sample of the 167 assemblies built through `rules_dotnet`

PowerToys has 243 C# projects in its solution. 44 compile XAML or depend on a
project that does and are out of scope; the reference build of the other 199
was extracted, and `scripts/emit_dotnet.py` wrote BUILD files for 167 of them,
all of which build under Bazel (the diff converges with 0 errors and every generated file matches). The full output is 169 BUILD files
and a 2 MB lock, so this directory carries **a sample**, cut from that run
with the emitter's `--only`:

| Target | Why it is here |
|---|---|
| `//src/common/ManagedCommon` | Compiled by `csc_assembly`: its resource is named after the root namespace (`ManagedCommon.CommonResources`), not the assembly (`PowerToys.ManagedCommon`), which rules_dotnet cannot express. Also a C#/WinRT projection of metadata Bazel builds from IDL |
| `//src/common/Common.UI` | A plain `csharp_library` that uses WPF: the WindowsDesktop reference pack through the lock, and the `...as.WindowsBase` dependencies that make rules_dotnet drop its own facade of that assembly |
| `//src/modules/MouseUtils/MouseJump.Models` and its `.UnitTests` | An MSTest project on Microsoft.Testing.Platform as a `csharp_test`: `bazel test` runs the same 31 tests as the reference program, all passing |
| `//bazel/winmd:PowerToys.Interop` and the others the sample references | `midl /winrt` + `mdmerge` over six IDL files, without building the component |

The method is in
[`docs/REFERENCE-msbuild-csharp.md`](../../docs/REFERENCE-msbuild-csharp.md);
the findings are §10 of the case study.

### The whole run

| | |
|---|---|
| C# compiles in the scoped reference build | 199 |
| Emitted and built by Bazel | 167 (16 through `csc_assembly`) |
| `diff.py` against the reference | converged, 0 errors; 151 warnings, all for the `NETSTANDARD*` defines rules_dotnet adds |
| `check_generated.py` | identical: 750 generated trees and resource sets, 867 files |
| Excluded: compiles XAML | 1 (`WorkspacesLauncherUI.UnitTests` links a WPF window in; found from the build log, not from its directory) |
| Not emitted | 31: 4 projects and their 27 dependents |

Not emitted, and why:

- `UITestAutomation.Next`, `Peek.Common`: `<COMReference>`. MSBuild generates
  `Interop.Shell32.dll` and others from type libraries registered on the
  machine; no rule here does. 24 UI-test projects depend on the first.
- `Microsoft.CmdPal.Ext.Shell`, `Microsoft.CmdPal.Ext.System`:
  `CsWin32RunAsBuildTask`. The generator runs as an MSBuild task and writes a
  source file; the 17 projects that use CsWin32 as an ordinary source generator
  are fine.

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

### Additional layout

The `.bzl` files under `bazel/dotnet/`, `bazel/winrt.bzl` and the updated
`bazel/windows_sdk.bzl` are the same files as in
[`examples/winget-cli`](../winget-cli/README.md#additional-layout), described
there. `nuget_lock.json` is the sample's part of the lock.

### Reproducing

The scoped reference build went through a solution filter listing the 199
projects, so that the solution's configuration mapping still applies:

```bash
msbuild a2b-scope.slnf -restore -p:RestorePackagesConfig=true \
    -p:Configuration=Release -p:Platform=x64 -m -bl:msbuild.binlog
python3 .../scripts/extract_msbuild.py msbuild.binlog . model.msbuild.json \
    --report msbuild.report.json --design-time
python3 .../scripts/nuget_assets.py msbuild.report.json <ref> nuget_lock.json --fetch-packs
python3 .../scripts/emit_dotnet.py msbuild.report.json nuget_lock.json . --reference-root <ref>
```

then build, `diff.py` and `check_generated.py` as in the winget-cli example.
Use a short Bazel output base (`--output_user_root=C:/b`): project names such
as `Microsoft.CmdPal.AdaptiveCards.IncrementalRendering.UnitTests` appear twice
in an output path.

## Known gaps

1. **One project.** Its dependencies on the rest of `src/common` are satisfied
   by a glob of headers, not by per-project targets.
2. **`version_gen.h` restates `src/Version.props`** in the BUILD file. MSBuild
   also reads `BUILD_SOURCEVERSION` / `GITHUB_SHA`; the Bazel equivalent is
   stamping.
3. **The runner is a host file** Bazel does not track, and three tests depend
   on the account running them.
4. **No precompiled header, host toolchain, vcpkg in a repository rule**: as in
   the winget-cli example.
5. **Code analysis (`/analyze`) is not run.** The reference build runs it on
   every compile; it is a diagnostics pass and is not mirrored.
