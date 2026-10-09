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
