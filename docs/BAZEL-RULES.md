# Bazel rules and versions: what we have actually used

Notes for the Bazel *target* side of a migration — which rulesets have been
exercised by the migrations in [EXPERIMENTS.md](../EXPERIMENTS.md), at which
versions, and the decisions behind reaching for a ruleset or writing rules by
hand. Nothing here is a recommendation in the abstract: read the "how far it was
exercised" column, because that is the whole content.

This file will degrade. Version ranges age, rulesets change hands, and the
entries below are dated by construction. Prefer re-checking over trusting it —
and see [Versions are resolved, not remembered](#versions-are-resolved-not-remembered),
which is the one rule here that does not age.

Last checked: the ranges below reflect the migrations as of this file's commit.

## Versions are resolved, not remembered

Two failure modes, both observed:

**An agent writes the version that was current when it was trained.** This
repo's Dolphin migration declared `rules_cc 0.0.17` and `platforms 0.0.10`. At
the time of writing, `rules_cc` was at 0.2.22 — 49 releases later. Nothing
failed, so nothing surfaced it. Resolve the version at migration time from the
registry (`https://bcr.bazel.build/modules/<name>/metadata.json` lists every
published version) or from the ruleset's tags, and never from recall.

**The version you declare is not the version you get.** MVS treats it as a
*floor*, and the binding floor is usually `bazel_tools` — built into Bazel
itself, so `.bazelversion` is part of the dependency specification. Same
one-line `MODULE.bazel` declaring `rules_cc 0.0.16`, three Bazel versions:

| Bazel | resolved `rules_cc` |
|---|---|
| 7.5.0 | 0.0.16 |
| 8.4.2 | **0.1.1** |
| 9.2.0 | **0.2.17** |

On 9.2.0 anything below 0.2.17 is inert. So record what resolved, alongside the
Bazel version (`bazel mod graph`; add `--include_builtin` to see `bazel_tools`),
and put `common --check_direct_dependencies=error` in `.bazelrc` so a stale
declared version fails the build instead of warning. Dolphin's `MODULE.bazel`
fails that check today. This bites version *upgrades* hardest: bumping Bazel
moves rulesets with no change to `MODULE.bazel`.

The same command shows the transitive cost. Dolphin declares **two** modules and
resolves **27**, including `rules_kotlin`, `rules_android`, `rules_swift` and
`rules_jvm_external` — in a C++ emulator, arriving via
`rules_cc → protobuf → …`. Worth knowing before it is a surprise.

Newest is not automatically right, either. The three Google C++ projects whose
existing Bazel builds we diffed against were all *behind* the latest `rules_cc`
when checked (0.2.14, 0.2.18, 0.2.19 vs. 0.2.22 published). What comparable
projects actually ship is usually a better signal than what the registry offers.

## Exercised by these migrations

Ranges, not recommendations. "Declared" is what a `MODULE.bazel` asked for;
"resolved" is what MVS produced and therefore what actually ran.

| ruleset | declared range | resolved | migrations | how far it was exercised |
|---|---|---|---|---|
| `rules_cc` | 0.0.16 – 0.2.22 | 0.2.14 – 0.2.22 | Dolphin; fmt, spdlog, TinyXML2, zlib; BoringSSL, Abseil, RE2 | Dolphin: `//...` green in 3 configurations, 1350 tests pass, action-graph parity 0 errors. Build re-verified green at declared 0.0.16 / 0.1.1 / 0.2.22; `compatibility_level = 1` and both `//cc:defs.bzl` and the per-rule `.bzl` files exist across that whole span |
| `platforms` | 0.0.10 – 1.1.0 | 1.0.0 – 1.1.0 | same | as above; only ever a transitive/constraint dep |
| `rules_jvm_external` | 6.7 | — | Guava (Maven frontend) | one point, coordinate deps only; the Maven frontend is argv-floor, so this is not a parity claim |
| `rules_license`, `googletest`, `google_benchmark`, `rules_python` | see note | — | BoringSSL, Abseil, RE2 | **not our choices** — these are what those projects' *own* Bazel builds declare (`rules_license` 1.0.0, `googletest` 1.17.0.bcr.2, `google_benchmark` 1.9.4/1.9.5, `rules_python` 1.7.0). We diffed against them; we did not select them |

Bazel itself: **7.5.0** (VSCode, pinned in `.bazelversion`) and **9.2.0**
(Dolphin). No migration here has needed a Bazel-version-specific workaround.
Every "resolved" column above is a fact about 9.2.0, not about the declared file.

## Written by hand instead of adopting a ruleset

The interesting cases. Both are C++/TS migrations where an obvious ecosystem
ruleset existed and was not used — with the reasoning, and the caveat that
neither was A/B tested.

### VSCode: `rules_ts` / `rules_esbuild` / `rules_nodejs` → 4 custom rules

The build is gulp + gulp-tsb + esbuild plugins; the Bazel side is `ts_program`,
`esbuild_bundle`, `vscode_app`, `nodegyp_module` (~500 lines of Starlark, ~1000
lines of Node wrappers), and a `MODULE.bazel` whose only external deps are two
`http_archive`s for Electron and Electron's node headers.

The deciding reason is **what kind of equivalence the migration is for**:

> `rules_ts` and `rules_esbuild` are designed for **content-equivalent** output —
> idiomatic settings, sensible defaults. This migration needed **byte-identical**
> output to what gulp-tsb + esbuild produce (banner, `sourceMappingURL` comment,
> contents-mapper and external-override plugins, `absWorkingDir`, TS boilerplate
> stripping). Fighting the rules to reproduce npm's exact bytes was going to be
> harder than owning the Starlark.

Four supporting reasons:

* **Every stage needed pre/post processing regardless.** `ts_program` runs tsc
  *plus* a non-TS asset copy *plus* the NLS transform; `esbuild_bundle` injects
  two plugins and post-processes the `.js` for path normalization and the
  sourcemap comment. Layered on `rules_ts` these become extra rules consuming and
  rewriting its outputs — a longer pipeline for the same result.
* **The project shape doesn't benefit.** `ts_project` is built around
  per-tsconfig granularity and project references; vscode is one monolithic
  ~7000-file program with no project refs, so the ruleset would have been used as
  a single opaque wrapper anyway.
* **Iteration speed on byte parity.** Chasing `__toESM(x, 1)` vs `__toESM(x)`
  was a one-line `preserveSymlinks` toggle plus a regex in the same file; a wrong
  `absWorkingDir` was one attribute. Through a ruleset those are config wrestling.
* **Dependency graph stays at two.** `rules_ts` + `rules_nodejs` would pull
  `aspect_bazel_lib`, `rules_js`, a pnpm-based npm toolchain, etc.

There is also a hard structural conflict, verified by reading
`rules_ts` 3.9.2 rather than assuming it. `ts_project` *does* expose
`source_map` / `out_dir` / `root_dir` attributes — but its validator
(`ts_project_options_validator.cjs`) **fails the build when an attribute
disagrees with the tsconfig on disk**, and separately rejects an `outDir` that
resolves above the package (*"not supported by ts_project because all output
files must be within the package output directory"*). vscode's build sets
`sourceMap: false, outDir: "../out/vs"` in `src/tsconfig.json` and then
*overrides* them programmatically at build time in `build/lib/compilation.ts`
(`options.sourceMap = true`, plus `rootDir`, `baseUrl`, `sourceRoot`, and a
`newLine` computed by reading `compilation.ts`'s own bytes). So the ruleset's
premise — the tsconfig is the source of truth and the BUILD file mirrors it — is
the opposite of this build's premise. Adopting it would mean editing the tsconfig,
which changes the reference build you are trying to match.

**The generalization**: an off-the-shelf rule encodes a premise about *where
configuration lives*. Ecosystem rules are mostly written for greenfield projects
where the checked-in config is canonical. A migration that must match an existing
build can only adopt such a rule if it shares that premise.

**What was given up:** per-file `ts_project` caching (the custom rule emits one
`declare_directory`), `rules_esbuild`'s runfiles integration (node_modules is
staged by hand), and community fixes for edge cases not yet hit.

**Caveat, stated because it matters:** this was not A/B tested. The choice was
made from "we know we need many hooks, and parity means unusual output" — both
of which held — but nobody can say `rules_ts` would have cost N more days. **If
parity is not required** and content-equivalent idiomatic output is acceptable,
`rules_ts` is probably the right call.

### Dolphin: Qt handled by hand, and a ruleset that was missed

Dolphin's Qt support is a repository rule (`third_party/qt/qt_detect.bzl`) that
discovers the host's Qt include roots, versioned private-header directory and
`moc` binary, plus a `dolphin_moc` macro wrapping moc in a genrule. No Qt ruleset
was considered — which was an oversight, not a decision.

Two Qt rulesets exist, and **the name in the registry is the one that fits a
CMake migration least**:

| | `Vertexwahn/rules_qt6` | `kklochkov/rules_qt` |
|---|---|---|
| in the BCR as `rules_qt` | **yes** — 0.0.7, 4 releases | **no** |
| own latest version | 0.0.7 | **2.0.1** |
| where Qt comes from | downloads a prebuilt Qt; README: *"only version 6.8.3 is tested and supported"* | `qt.local_repo(path=…)` runs **qmake** to discover the installation, or `remote_repo` |
| codegen | moc via genrule | moc / uic / rcc / qml as rules with providers |

CMake resolves Qt with `find_package(Qt6 …)`, i.e. against the *host*. So the
fitting choice is `qt_local_repo`, which asks qmake the same question — and that
is in the ruleset that is **not** in the BCR under `rules_qt`. Querying the
registry for `rules_qt` returns the prebuilt-single-version one, which would have
pinned Qt 6.8.3 and turned the migration into a different build.

Neither has been used here, so this is a pointer for whoever hits Qt next, not a
recommendation. The transferable parts:

* **BCR presence is not fitness, and BCR absence is not nonexistence.** The
  registry is a distribution channel, not a curated index. Search wider, and
  expect more than one ruleset per domain.
* **The fitness test is the migration's own test:** does the ruleset resolve the
  dependency the way the reference build does? A ruleset that downloads a pinned
  SDK where the original build queried the host is a *scope change* — sometimes
  an improvement, but it must be a stated decision, because it will move the
  parity diff for reasons that are not bugs.

## `rules_cc` on Windows: the MSVC toolchain's fixed points

Checked on Bazel 9.2.0 with `rules_cc` resolved to 0.2.17, against Visual
Studio 2022 Build Tools (MSVC 14.44) and Visual Studio 2026 (MSVC 14.51) on one
machine. These are properties of the auto-configured MSVC toolchain an MSBuild
migration lands on; the MSBuild-specific guidance is in
[REFERENCE-msbuild.md](REFERENCE-msbuild.md).

* **It picks the newest installation unless told otherwise.** With both
  installs present Bazel chose 14.51 while MSBuild from the Build Tools had
  built the reference with 14.44. `--repo_env=BAZEL_VC=<...\VC>` plus
  `BAZEL_VC_FULL_VERSION` and `BAZEL_WINSDK_FULL_VERSION` pin it; the compile
  action's compiler path and `INCLUDE` environment then show the pinned
  versions.
* **Its default compile flags cannot be removed.**
  `--features=-default_compile_flags` fails analysis ("unconditionally implies
  feature"). Every compile carries `/DNOMINMAX /D_CRT_SECURE_NO_DEPRECATE
  /D_CRT_SECURE_NO_WARNINGS /bigobj /Zm500 /EHsc /wd4351 /wd4291 /wd4250
  /wd4996 /D_WIN32_WINNT=0x0601`, plus `/std:c++17` on C++ sources. Only the
  last define is configurable: `BAZEL_WIN32_WINNT` replaces it and must hold
  the entire flag (`/D_WIN32_WINNT=0x0A00`; a bare `0x0A00` is passed to the
  compiler as is).
* **They can be undone, though.** The variable accepts any flag, so
  `--repo_env=BAZEL_WIN32_WINNT=/U_WIN32_WINNT` removes that define, and
  `--copt=/UNOMINMAX --copt=/U_CRT_SECURE_NO_DEPRECATE
  --copt=/U_CRT_SECURE_NO_WARNINGS` removes the others (a `/U` after the `/D`
  wins; each compile prints a D9025 warning). This matters when the reference
  build defines none of them: a source that defines `NOMINMAX` itself is a
  C4005 redefinition, which `/WX` turns into an error.
* **The static C runtime is a feature**, `features = ["static_link_msvcrt"]`
  (`/MT`, and `libcmt.lib` as the default library). `copts = ["/MT"]` changes
  the compile and leaves the link asking for `msvcrt.lib`.
* **There is no resource compiler and no precompiled-header support.** A `.rc`
  file needs its own rule (the `.res` is a link input); `/Yc` and `/Yu` have no
  equivalent, so every source parses its `pch.h`.
* **A test rule's executable can be a `.bat`.** A custom `test = True` rule that
  writes a batch launcher runs under `bazel test`; files are found through
  `RUNFILES_MANIFEST_FILE`, since Windows has no runfiles tree by default.
* **The linker param file cannot be disabled either.**
  `--features=-linker_param_file` fails the same way, while
  `-compiler_param_file` is accepted. `aquery --include_param_files` reports
  the file's content, which is what the Bazel extractor reads.
* **A `cc_library` builds an archive and a DLL.** Under `//...` it emits
  `CppArchive` (`name.lib`) and `CppLink` (`name_0.dll` plus `name.if.lib`).
  It is still a library, not a DLL deliverable; the extractor keeps it
  `static_library`. A real DLL is `cc_binary(linkshared = True)` (or
  `cc_shared_library`).
* **Link defaults**: `/SUBSYSTEM:CONSOLE /MACHINE:X64 /DEFAULTLIB:msvcrt.lib`,
  `/OPT:ICF /OPT:REF` under `-c opt`, and a generated empty `/DEF:` for
  libraries. A project's own `/SUBSYSTEM:` in `linkopts` comes later on the
  line and wins.

### What the emitted C++ targets rely on

Found while taking 16 PowerToys projects through `emit_cc.py` (rules_cc 0.2.17,
Bazel 9.2):

- **`additional_compiler_inputs`** on `cc_library` / `cc_binary`, with
  `$(execpath ...)` in `copts`: how a compile is handed the `.pch` another
  action wrote (`/Yu`, `/Fp`). The `/Yc` compile itself is a rule
  (`pch.bzl`) that takes its command line from `cc_common`, so that Bazel's own
  options are the same on both sides.
- **`cc_common.create_linking_context_from_compilation_outputs`**: turns the
  object of such a rule into a library like any other (with `alwayslink` for a
  program, whose project hands every object to the linker).
- **`textual_hdrs`** exists on `cc_library` only. A source another source
  includes goes into a small library the binary depends on.
- **A target's static libraries come before its `linkopts` on the link line.**
  MSBuild has system libraries first. Symbols that two import libraries offer
  (kernel32 and the API sets) bind to another DLL.
- **Compile actions use a parameter file aquery does not report**
  (`--include_param_files` has it for links only). Query with
  `--features=-compiler_param_file`, or every native target looks like an
  aggregate without sources.
- **`attr.string_keyed_label_dict`** (destination to target) and
  `ctx.actions.symlink` are what a rule needs to lay files of many targets out
  in one directory (`layout.bzl`); with `startup --windows_enable_symlinks` the
  links are real ones.
- **A library named like a system library** (`Version.lib`) satisfies a
  `#pragma comment(lib, "version.lib")` for the linker. Not a rules_cc matter,
  but it surfaces here first: see the field notes of REFERENCE-msbuild.md.

## `rules_dotnet` (C#)

Exercised: 0.22.2 on Bazel 9, Windows, .NET SDK 10.0.400, against winget-cli
(22 compiles) and PowerToys (167 compiles). The full account, with the
templates that go around it, is
[REFERENCE-msbuild-csharp.md](REFERENCE-msbuild-csharp.md). What to know before
writing a `csharp_library` by hand:

* **`--enable_runfiles` is required on Windows**; without it a program does not
  find its own assemblies.
* **The compile action is `CSharpCompile`, always with a parameter file.**
  `aquery --include_param_files` has the complete command line. A library with
  `internals_visible_to` has a second, `/refonly` action.
* **Defaults that are not csc's**: `/warn:3` (csc: 4), `/nullable:disable`,
  `/langversion` of the SDK, and it always passes `/unsafe-`, `/checked-`,
  `/deterministic+`, `/highentropyva+`, `/nowarn:CS1701,CS1702`. State what the
  reference build has.
* **Target frameworks have no platform.** `net8.0-windows10.0.26100.0` is
  `net8.0` plus defines and references you provide. Its NuGet rule drops
  package assets filed under a framework it does not know; lock what MSBuild
  resolved instead (`nuget_assets.py` + `nuget_assets.bzl`).
* **It decides part of the command line, and that part can be wrong for a
  project**: embedded resources are always named `<assembly>.<path>`; a .NET
  Framework target gets the whole reference pack; `project_sdk = "web"` gets
  every analyzer of the ASP.NET pack; `winexe = True` still compiles with
  `/target:exe`; the base pack's facade `WindowsBase.dll` is passed next to
  WindowsDesktop's real one. Each has a workaround that does not patch
  rules_dotnet; for the first three it is `csc_assembly.bzl`, a compile with
  exactly the reference build's inputs that hands its result back through
  `import_library`. For the resource names and `winexe` there are also
  patches to rules_dotnet itself, in
  [`patches/rules_dotnet/`](../patches/rules_dotnet/README.md).
* **`runtimeconfig.json` names `Microsoft.NETCore.App` only.** A program that
  loads WPF or WinForms does not start as built.

## `rules_foreign_cc`: not for the code under migration

It runs the original build system (CMake, make, autotools) inside a Bazel action.
That produces artifacts, but the compile and link actions happen inside an opaque
wrapper, so `bazel aquery` cannot see them — and the action-graph diff this whole
method rests on has nothing to compare. Adopting it for the subject of the
migration removes the only signal that the migration is faithful.

The boundary is worth stating precisely: the objection is to losing the action
graph **for code you are migrating**. Driving a foreign build for a leaf
prebuilt artifact you never claimed parity on is a different thing — VSCode's
`nodegyp_module` shells out to `node-gyp rebuild` for native `.node` modules, and
nothing is lost, because those binaries were never part of the parity claim.

## Checklist for the Bazel side of a migration

1. Resolve every version at migration time; do not write one from memory.
2. Prefer what comparable projects ship over what the registry says is newest.
3. Search beyond the BCR, and expect several rulesets per domain.
4. For each candidate ruleset, ask whether it shares the reference build's premise
   about where configuration lives, and whether it resolves dependencies the same
   way (host vs. pinned download).
5. Decide what equivalence you need — byte-identical or content-equivalent —
   before choosing. Ecosystem rules target the latter.
6. Weigh the transitive graph (`bazel mod graph`), not just the direct dep.
7. Keep `rules_foreign_cc` away from the code under migration.
8. When the migration converges, run `bazel mod graph` and record the **resolved**
   versions. That is the only version list worth keeping.
