# MSBuild Migration Status

This document tracks the status of MSBuild migration support in `any2bazel`.

## Known to work
- Binlog JSON extraction format (using BinlogToJson) appears stable.
- Extracting MSBuild target data (like C# compilations) into the action model.
- `rules_dotnet` Bazel side extraction for `CSharpCompile` mnemonics works and correctly represents the source files.

## Known to not work
- `BinlogToJson` relies purely on `ClCompile`/`Link`/`Csc` task execution. Therefore, if a binlog is generated during an incremental build where targets are up-to-date, those projects will be silently completely dropped from the migration model. Generating binlogs requires forcing a full rebuild (e.g. `msbuild -t:Rebuild`) or `msbuild -t:Clean` followed by `msbuild -t:Build`. Note that some projects (like `winget-cli`) may wipe out custom environments like `vcpkg_installed` during a `Rebuild` without automatically reinstalling them, which requires manually doing `Clean -> vcpkg install -> Build` instead.
- Projects with missing dependencies (like missing `yaml.h` or `json/json.h` due to unresolved vcpkg or submodules) will fail the MSBuild run early. If `ClCompile` doesn't run for a target due to prerequisite failures, those files/targets are dropped from the binlog, leading to an incomplete `.json` extraction (e.g. `winget-cli` outputting only ~3KB json when it fails early).

## Ideas for Improvement
- **Binlog Extraction Shortcomings**: The Rebuild requirement means we must be able to successfully build the project without a single error (including setting up all required dependencies).
  - *Idea 1*: Improve the skill's ability to automatically determine the correct way to build a project and install dependencies (e.g. following `Developing.md` guidelines like `vcpkg` bootstrap).
  - *Idea 2*: Pursue a "dry-run" MSBuild execution method that logs all compiler tasks and command lines without actually requiring a successful compilation. (e.g., experimenting more with `/p:SkipCompilerExecution=true` for C# or finding C++ equivalents).

## Notes
- Tested `any2bazel` on `winget-cli` (Case Study):
  - A raw `msbuild -t:Rebuild` without setup failed to find `yaml.h` and `json.h`, confirming that `any2bazel` requires a fully pristine, working build environment to even start the migration loop.
  - Successfully proved out the migration by manually fixing the build environment: `winget-cli` has a custom `CleanVcpkg` MSBuild target that deletes `vcpkg_installed` during `Rebuild` without triggering a reinstall.
  - Fix: Ran `msbuild -t:Clean`, executed `vcpkg install --x-manifest-root=src --triplet x64-release` manually to build dependencies like `libyaml` and `jsoncpp`, and then captured a full binlog via `msbuild -t:Build -bl:msbuild.binlog`.
  - The `any2bazel` tools successfully digested the resulting 961 KB `msbuild.json` file and mapped 36 targets to the canonical `extracted.json` model, proving that the main winget cli and unit tests can be extracted.
- Testing against `winget-cli` projects `WinGetUtilInterop` and `WinGetTestCommon` using `rules_dotnet` showed that MSBuild C# extraction and Bazel aquery models converge perfectly on source files (once the path normalization bug in `extract_msbuild.py` was fixed).
- Discovered that `rules_dotnet` versions before 0.17 fail on Bazel 9 with `incompatible_use_toolchain_transition`. Upgrading to 0.22.2 resolved it.
