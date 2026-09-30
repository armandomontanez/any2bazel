# Initial plan: MSBuild → Bazel migration frontend

## Context

`any2bazel` today supports three frontends: **CMake** (mature, the validated
path), **Maven** (early, argv-floor only), and **npm/esbuild** (experimental,
capture-based). All three feed a shared action-based IR
(`scripts/model.py`) that a language/mnemonic-aware differ compares against
`bazel aquery` output.

MSBuild is a large gap. Real-world Windows/C++ projects (Microsoft Terminal,
PowerToys, WinGet, WinAppSDK samples) are MSBuild+MSVC, and porting them to
Bazel today would mean hand-writing the whole loop the CMake path automates.
This plan adds an **MSBuild frontend** so that path exists.

**Chosen scope for the first cut** (from user answers):
- **Reference source:** MSBuild binary log (`.binlog`), parsed via a small .NET
  helper that emits JSON. Closest analog to the CMake File API — it exposes
  fully evaluated properties, resolved `ClCompile`/`Link`/`Csc`/`Midl` task
  invocations, and per-file item metadata. `.binlog` is what MS's own tooling
  (`msbuild.exe /bl`, StructuredLogViewer) consumes; both `Microsoft.Build.Logging.StructuredLogger`
  and Microsoft's own `Microsoft.Build.Framework` can read it.
- **Language coverage:** **Mixed C++ and C# from the start.** Extract both
  `.vcxproj` (cl.exe/link.exe) and `.csproj` (csc.exe) actions in the same
  pass. Two mnemonics on the Bazel side (`CppCompile`, `CSharpCompile`); two
  target rulesets (`rules_cc`, `rules_dotnet`).
- **First validation target:** **PowerToys** (`/Users/armando/development/projects/extra/PowerToys`).
  120 `.vcxproj` + 248 `.csproj`, vcpkg for native deps, centralized NuGet via
  `Directory.Packages.props`, WinUI/XAML, custom PowerShell pre-build steps.
  Aggressive `exclude_targets` on the first pass is expected and planned for.

The intended outcome: a fourth frontend, callable through the same
`/any2bazel` skill, that lets the LLM iterate BUILD files against a captured
MSBuild reference until action-graph parity converges — just as the CMake path
does today.

## Approach

Reuse the existing spine (`scripts/model.py`, `scripts/reconstruct.py`,
`scripts/diff.py`, `scripts/triage.py`, `scripts/config.py`) and add:

1. **`scripts/msbuild_reader/`** — a small self-contained .NET tool that reads
   a `.binlog` and emits a stable JSON schema (the MSBuild equivalent of the
   CMake File API reply). Vendored as source; built once with `dotnet publish`;
   the resulting single-file binary is checked in per-OS or built on demand.
   *Why not read the binlog directly from Python:* the format is a versioned
   binary stream tightly coupled to MSBuild internals; using MS's own
   `StructuredLogger` library is the only way to stay forward-compatible with
   Visual Studio 17.x / 18.x MSBuild versions.
2. **`scripts/extract_msbuild.py`** — reads the JSON the .NET helper emits and
   synthesizes the canonical model. Analogous to `scripts/extract_cmake.py`.
3. **Model extensions in `scripts/model.py`:**
   - Add `BuildSystem.MSBUILD = "msbuild"`.
   - Add mnemonics `CSharpCompile` (analog of `JavaCompile`) and — later —
     `MidlCompile`, `ResourceCompile`. Only `CSharpCompile` for the MVP.
   - No structural changes: MSBuild fits the action model directly. cl.exe is a
     compile action per source (like CMake compile groups); csc.exe is one
     action per project/source-set (like javac).
4. **Bazel-side toolchain: clang-cl via hermetic-llvm.** Bazel targets Windows
   through [hermeticbuild/hermetic-llvm](https://github.com/hermeticbuild/hermetic-llvm)
   configured to use `clang-cl`, the MSVC-compatible driver. Both sides then
   speak **MSVC flag syntax natively** — MSBuild drives `cl.exe`, Bazel drives
   `clang-cl.exe`, both accept `/D`, `/I`, `/std:c++20`, `/EHsc`, `/MT[d]`,
   `/W4`, `/Zi`, `/O2`, etc. This is the load-bearing decision of the plan:
   **no cross-dialect flag translation**, so no lossy GNU-equivalence layer,
   which was the biggest correctness risk.
5. **Differ / reconstruct extensions in `scripts/reconstruct.py` +
   `scripts/canonicalize.py`:**
   - **Canonical form is MSVC-style, not GNU-style.** Because both sides emit
     MSVC flags, the differ compares them as-is. What the canonicalizer *does*
     do:
     - Tokenize MSVC flag pairs consistently: `/D FOO=1` and `/DFOO=1` and
       `-DFOO=1` (clang-cl accepts the `-D` alias) all normalize to the same
       token. Same for `/I`/`-I`, `/Fo`/`-Fo`, `/std:c++20`/`-std:c++20`.
     - Strip MSVC-side build-system noise analogous to what the current
       canonicalizer strips for GNU: response-file paths, `/nologo`,
       `/FS` (multi-process serialization), `/Fd*.pdb`, `/Fo*` output paths,
       `/showIncludes` (if hermetic-llvm sets it for dep tracking), the
       toolchain wrapper prefix.
     - Sort defines; preserve include order (same policy as today's GNU path).
     - Warning-set differences (`/W4` vs `/W3`, `/wd####` per-warning
       suppressions) go through `any2bazel.json.ignore` — same policy as
       today's `-W*` prefixes.
   - **Correctness flags stay hard errors** (matching the existing rule): any
     divergence on `/std:*`, `/EH*`, `/MT[d]`/`/MD[d]`, `/GR[-]` (RTTI on/off),
     `/await`, `/permissive[-]`, `/Zc:*` (conformance switches) surfaces as
     `flags_diff`. These are the MSVC equivalents of `-std=*`, `-fexceptions`,
     `-fno-rtti`, etc., that today's canonicalizer refuses to hide.
   - **C# comparison:** **project-wide TU-set union of `.cs` sources**, mirroring
     the Maven/Java `JavaCompile` handling. Grouping-agnostic (a `.cs` file
     compiled on both sides = match). New diff kinds: `missing_cs_src`,
     `extra_cs_src`, and later `csharp_flags_diff` once the argv floor is
     interesting enough to warrant it.
6. **`SKILL.md` updates** — add MSBuild to the frontends table and the "Other
   frontends" section, with a documented procedure block mirroring the CMake
   one (extract → generate BUILD → aquery → diff → triage → fix → loop). Call
   out the **hermetic-llvm + clang-cl** requirement on the Bazel side: an
   MSBuild migration configured against a native GCC/Clang toolchain will
   generate `flags_diff` noise on nearly every TU (dialect mismatch), so the
   `.bazelrc` template in step 3 must declare hermetic-llvm and pin
   `--compiler=clang-cl` on Windows.
7. **`docs/BAZEL-RULES.md` updates** — record what rulesets get exercised on
   the Bazel side of the MSBuild loop. Expected new rows: `rules_cc` (via
   hermetic-llvm's clang-cl toolchain, `--config=windows` gating),
   `hermetic-llvm` itself (record the resolved version and how the toolchain
   is registered), and `rules_dotnet`. Explicitly flag the
   WinRT/XAML/vcpkg/MIDL story as unresolved.

### Concretely for PowerToys as the first case study

The plan does **not** attempt to converge PowerToys' entire graph in the MVP.
It aims to converge a **carved-out core** and record everything else in
`any2bazel.json` `exclude_targets`, exactly as the CMake path handles vendored
subtrees. Expected exclusions on day one:

- All `.wapproj`-adjacent packaging targets (installers, MSIX bundling).
- All XAML/WinUI projects until `xaml_compile` / `midl` / `cswinrt` codegen has
  a story. That is a large fraction of the C# side.
- All targets whose only build step is a PowerShell `Exec` (the `runner`
  pre-build resx→rc conversion, `PackageIdentity` sparse MSIX generation) —
  same reason CMake `add_custom_command`/codegen is out of MVP scope.
- vcpkg-only C++ targets whose dep closure doesn't resolve without a
  vcpkg-manifest → bzlmod story.

That still leaves a substantial slice — most of `common/`, IPC libraries, small
utilities without XAML — to prove the loop.

## Key files

**New files to add** (all under this repo, `/Users/armando/projects/any2bazel`):

- `scripts/msbuild_reader/BinlogToJson.csproj` and `Program.cs` — the .NET
  binlog→JSON helper (single file, `dotnet publish -r <rid> --self-contained`).
- `scripts/extract_msbuild.py` — pattern: mirror `scripts/extract_cmake.py`
  line-for-line. Same shape: read a structured reply, walk targets, synthesize
  one `Action` per compile invocation, attach `Dependency` annotations from
  resolved `<Reference>`/`<ProjectReference>`/`<PackageReference>`/`<Link>` items.
- `tests/test_extract_msbuild.py` — fixtures generated once from a tiny
  `.binlog` recorded from a hello-world `.vcxproj` + `.csproj`. Same fixture
  pattern as `tests/test_extractors.py`.
- `docs/CASE-powertoys-migration.md` — placeholder, filled in as the
  migration progresses (mirroring `CASE-ladybird-migration.md` and
  `CASE-vscode-migration.md`).

**Files to modify:**

- `scripts/model.py` — add `BuildSystem.MSBUILD`.
- `scripts/reconstruct.py` and `scripts/canonicalize.py` — add MSVC flag
  normalization; add `CSharpCompile` grouping (copy `JavaCompile` handling).
- `scripts/diff.py` — add `missing_cs_src` / `extra_cs_src` kinds.
- `scripts/triage.py` — add histogram bucketing for the new kinds (mostly
  automatic if the diff kinds follow the same shape).
- `scripts/extract_bazel.py` — recognize `CSharpCompile` mnemonic on the
  Bazel side (rules_dotnet emits it) and map it into the model. Today it
  handles `CppCompile`, `CppLink`, `CppArchive`, `Javac`, `TsCompile`.
- `SKILL.md` — new procedure block for the MSBuild path; add row to the
  frontends table.
- `docs/BAZEL-RULES.md` — new section for what got exercised.

**Existing utilities to reuse (do not duplicate):**

- `scripts/config.py` — `any2bazel.json` schema is already the right shape
  (`target_map`, `dep_map`, `exclude_targets`, `ignore.*`). No new fields
  needed for MVP.
- `scripts/serialize.py`, `scripts/model.py` — no shape changes beyond the
  enum addition.
- The CMake `_classify` heuristic in `extract_cmake.py:_classify` is the
  template for role inference; port it to look at `.vcxproj` project types
  and `<Import>` chains instead of CMake target types.

## Verification

End-to-end, in order:

1. **Unit-level.** Build a hello-world MSBuild solution (one `.vcxproj` +
   one `.csproj`), capture a `.binlog`, run
   `scripts/msbuild_reader` + `scripts/extract_msbuild.py`, snapshot the
   resulting `model.msbuild.json`. Assert against a golden fixture in
   `tests/test_extract_msbuild.py`. Run the existing test battery
   (`tests/test_engine.py`, `tests/test_extractors.py`, ...) unchanged —
   nothing about them should regress.

2. **Round-trip on a tiny hand-written case.** Author a Bazel BUILD for the
   hello-world project by hand (`cc_binary` + `csharp_binary`) with a
   `MODULE.bazel` that pulls hermetic-llvm and registers its clang-cl
   toolchain. Run `bazel aquery` + `extract_bazel.py`, then `diff.py`. Expect
   `converged: true` after zero rounds (both sides trivial, both emit MSVC
   flag syntax). This proves the differ handles both mnemonics on both sides
   AND that the clang-cl argv comes out MSVC-shaped. **If aquery here emits
   GNU-style flags instead of `/…` flags, the toolchain isn't wired right —
   fix that before proceeding, because every subsequent diff will be noise.**

3. **PowerToys carve-out.** Pick one `.vcxproj` from
   `/Users/armando/development/projects/extra/PowerToys/src/common/` that
   has no PowerShell/XAML/vcpkg dependencies. Follow the full procedure end
   to end — expect several rounds of `flags_diff`/`includes_diff` and one
   or two rounds of MSVC-normalization tuning in the canonicalizer.
   Record findings in `docs/CASE-powertoys-migration.md`.

4. **Bazel version pinning.** As `docs/BAZEL-RULES.md` warns, resolve
   versions at migration time — do not hard-code `rules_cc` or
   `rules_dotnet` versions in the skill's docs. Run `bazel mod graph
   --include_builtin` after convergence and record the resolved versions.

5. **Regression signal.** After all changes, the full existing test battery
   must still pass:
   ```
   python3 tests/test_engine.py && python3 tests/test_extractors.py \
     && python3 tests/test_maven.py && python3 tests/test_extract_npm.py \
     && python3 tests/test_triage.py && python3 tests/test_configure.py \
     && python3 tests/test_extract_msbuild.py
   ```

## Known unknowns / follow-ups (not in the MVP)

Recorded now so they don't ambush the first migration:

- **vcpkg manifest resolution.** No plan to auto-resolve `vcpkg.json` into
  bzlmod. On the first pass, vcpkg-consuming targets go in
  `exclude_targets` or the operator hand-writes `MODULE.bazel` entries.
- **XAML / WinUI / MIDL / CsWinRT.** All configure-time/build-time codegen —
  same category as CMake `configure_file` and `add_custom_command`, which are
  explicitly out of MVP scope per `SKILL.md`. Documented, deferred.
- **Custom `<Exec>` targets.** PowerToys uses PowerShell for resx→rc conversion
  and MSIX authoring. Out of scope, `exclude_targets` them.
- **Precompiled headers (`pch.h`).** MSVC's `/Yu`/`/Yc` model doesn't match
  `rules_cc`'s ergonomics; treat as a canonicalizer normalization
  (strip `/Yu*`, `/Yc*`, `/Fp*`) rather than modeling it. Any real PCH benefit
  is on the Bazel side, not something to diff.
- **Windows toolchain on the Bazel side.** Uses hermetic-llvm's `clang-cl`
  (not the stock MSVC toolchain in `rules_cc`, and not the GCC/Clang toolchain
  used elsewhere in this repo). This keeps the flag syntax on both sides
  identical — MSBuild → `cl.exe`, Bazel → `clang-cl.exe`, both accept the same
  `/…` flags. Expect *behavioral* differences between `cl.exe` and `clang-cl`
  (a small number of MSVC-specific pragmas/intrinsics clang-cl doesn't
  implement) that surface as compile errors at parity time, **not** as
  `flags_diff` noise. Toolchain-injected flags that clang-cl adds and cl.exe
  doesn't (or vice versa) get `ignore.flags_prefixes` entries, same policy as
  the CMake path.
- **Directory.Build.props / Directory.Packages.props auto-imports.** MSBuild's
  implicit `Directory.Build.props` walking (`.` up to root) is fully evaluated
  in the binlog, so extraction sees the final flag set. The plan does *not*
  attempt to reconstruct which prop file contributed which flag — that
  information is discarded in the binlog and reconstructing it would be
  duplicative of MSBuild's own evaluator.
