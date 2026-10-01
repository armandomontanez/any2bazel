## Goal Description
The goal is to extend `any2bazel` to support migrating MSBuild-based Visual Studio projects (`.sln`, `.vcxproj`, `.csproj`) to Bazel, providing a fourth frontend alongside CMake, Maven, and npm. The implementation will extract build graph information and compiler flags using MSBuild Binary Logs (`.binlog`), and map them into the `any2bazel` canonical model.

A critical load-bearing decision of this design is that the Bazel side will be configured to use an **MSVC-compatible toolchain**. This can be the native Visual Studio `cl.exe` (when migrating directly on a Windows machine) or **hermetic-llvm with clang-cl** (for cross-compilation from Linux/macOS). This ensures both MSBuild and Bazel natively speak MSVC flag syntax (`/D`, `/I`, `/O2`), eliminating the need for a risky, lossy translation layer between GNU and MSVC dialects.

## Design Decisions
- **MSVC-Compatible Toolchain**: Migrations must use either native `cl.exe` (via standard `rules_cc` on Windows) or `clang-cl` (via `hermetic-llvm`). We will not support extracting MSBuild into GNU or MinGW style GCC/Clang Bazel toolchain.
- **Precompiled Headers (PCH)**: MSVC PCH flags (`/Yu`, `/Yc`, `/Fp`) are treated purely as optimization mechanics and do not affect correctness. They will be stripped during flag canonicalization and ignored in the diff.

## Proposed Changes

### Core Extraction Engine
The extractor will read `.binlog` files via a C# helper to bypass MSBuild property evaluation complexity.

#### [NEW] scripts/msbuild_reader/BinlogToJson.csproj & Program.cs
A small, self-contained .NET tool that reads a `.binlog` using `Microsoft.Build.Logging.StructuredLogger`.
- Parses evaluated `ClCompile`, `Link`, and `Csc` task invocations.
- Outputs a stable JSON schema representing targets, sources, flags, and references.
- Built once via `dotnet publish` and checked in or built on-demand.

#### [NEW] scripts/extract_msbuild.py
- Coordinates the extraction process (analogous to `extract_cmake.py`).
- Reads the JSON from `BinlogToJson`, synthesizing `Action`s (`CppCompile`, `CSharpCompile`, `CppLink`).
- Attaches `Dependency` annotations based on resolved `<Reference>`, `<ProjectReference>`, and `<PackageReference>` items.

---

### Canonical Model & Reconstruction
Update the core IR to understand MSBuild, C#, and MSVC flag canonicalization.

#### [MODIFY] scripts/model.py
- Add `BuildSystem.MSBUILD`.
- Add `CSharpCompile` to known mnemonics (alongside `JavaCompile`).

#### [MODIFY] scripts/canonicalize.py
- **MSVC Tokenization**: Normalize flag prefix variations (e.g., `/D FOO=1`, `/DFOO=1`, and `-DFOO=1` all become `-DFOO=1` or `/DFOO=1` consistently).
- **MSVC Noise Stripping**: Strip MSVC driver mechanics and pure noise (`/nologo`, `/FS`, `/Fd*.pdb`, `/Fo*`, `/showIncludes`, `/Yu*`, `/Yc*`).
- **Correctness Flags**: Treat strict divergence on `/std:*`, `/EH*`, `/MT[d]`, `/MD[d]`, `/GR[-]`, `/await`, `/permissive[-]`, `/Zc:*` as hard errors that surface as `flags_diff`.

#### [MODIFY] scripts/reconstruct.py
- Add `CSharpCompile` to `_COMPILE_MNEMONICS`.
- Introduce a C# compile group approach mirroring Java (project-wide TU-set union of `.cs` sources).
- Update `_source_from_compile_args` and `_is_driver_token` to handle MSVC positional sources and driver flags.

#### [MODIFY] scripts/diff.py & scripts/triage.py
- Add `missing_cs_src` and `extra_cs_src` diff kinds for C# comparisons.
- Update histogram bucketing in triage to account for these new C#-specific divergence types.

#### [MODIFY] scripts/extract_bazel.py
- Recognize the `CSharpCompile` mnemonic emitted by `rules_dotnet` and map it properly.

---

### Documentation & Verification
Document the workflow and update the known supported rulesets.

#### [MODIFY] SKILL.md
- Add MSBuild to the frontends table.
- Document the MSBuild extraction procedure (binlog generation).
- Explicitly note the requirement to use an MSVC-compatible toolchain (native `cl.exe` on Windows or `hermetic-llvm`/`clang-cl` for cross-compilation).

#### [MODIFY] docs/BAZEL-RULES.md
- Record usage of `rules_dotnet`, `rules_cc` (under `--config=windows`, using native MSVC or `hermetic-llvm`).

#### [NEW] docs/CASE-powertoys-migration.md
- Placeholder for tracking the PowerToys case study.

## Verification Plan

### Automated Tests
- **[NEW] tests/test_extract_msbuild.py**: Create fixtures using a tiny "hello world" `.binlog` (C++ and C#) and assert that `model.msbuild.json` matches the golden schema.
- **[MODIFY] Existing Battery**: Ensure `test_engine.py`, `test_extractors.py`, and `test_canonicalize.py` pass without regressions. Run:
  `python3 tests/test_engine.py && python3 tests/test_extract_msbuild.py`

### Manual Verification
1. **Hello-World Round-Trip**: Extract the "hello world" project, author a hand-written `BUILD` file and `MODULE.bazel` using `hermetic-llvm`. Run `bazel aquery` and `diff.py` to prove that `converged: true` is reached in zero rounds.
2. **PowerToys Core Carve-Out**: Extract the MSBuild graph for `PowerToys/src/common/` (specifically choosing a `.vcxproj` with no XAML, WinUI, MIDL, or PowerShell dependencies). Exclude unsupported targets in `any2bazel.json`. Iterate `diff.py` against a Bazel build to ensure `flags_diff` reduces to zero after tuning the canonicalizer.
