# PowerToys → Bazel: the runner, two WinUI 3 apps and a WPF app

[`workspace-ui/`](workspace-ui/) is a Bazel workspace overlay for a slice of
[PowerToys](https://github.com/microsoft/PowerToys) that goes through every
kind of project its UI tools are made of: native libraries and module DLLs,
C++/WinRT components, C# libraries, WinUI 3 XAML with its resource index, WPF
XAML, and programs that run from the directory MSBuild would have given them.

| | |
|---|---|
| C++ (17 projects) | the runner (`PowerToys.exe`), the AlwaysOnTop program, 3 module DLLs (Hosts, AlwaysOnTop, ColorPicker), 3 C++/WinRT components (`PowerToys.Interop`, `GPOWrapper`, `ZoomItSettingsInterop`), 9 static libraries |
| C# (14 projects) | **Hosts** and **ColorPickerUI** (WinUI 3 programs) with `HostsUILib` and `Common.UI.Controls` (XAML libraries); **FancyZonesEditor** (WPF program); 9 libraries they reference |

Every BUILD file was written by `emit_cc.py` and `emit_dotnet.py` from the
report of an MSBuild build of exactly these projects. Nothing was edited by
hand. The guides: [REFERENCE-msbuild.md](../../docs/REFERENCE-msbuild.md) for
C++, [REFERENCE-msbuild-xaml.md](../../docs/REFERENCE-msbuild-xaml.md) for XAML.

**This is a slice.** 31 of PowerToys' 353 projects. It shows that each
mechanism works on real projects; it is not the product.

**What it achieves**, in a clone that never held MSBuild output:

- `bazel build //src/... //bazel/...` succeeds.
- `bazel run //src/modules/Hosts/Hosts:PowerToys.Hosts` opens the *Hosts File
  Editor* window, as the MSBuild-built program does. The window's markup comes
  out of the resource index Bazel built; the program loads native DLLs Bazel
  built.
- `bazel run //src/modules/colorPicker/ColorPickerUI:PowerToys.ColorPickerUI`
  starts Color Picker, the second WinUI program of the shared `WinUI3Apps`
  directory. It shows nothing until it is activated (Win+Shift+C, or the
  event the runner signals); signalled that event, it puts its picker overlay
  on screen, as the MSBuild-built program does.
- `PowerToys.FancyZonesEditor.exe` starts and shows the dialog the reference
  program shows when started without FancyZones ("Editor data parsing error").
- `diff.py` reports `CONVERGED errors=0 warnings=19` against the MSBuild
  Release|x64 reference, C++ and C# together (the warnings: defines rules_dotnet
  adds, and boilerplate sources in the intermediate XAML assemblies).
- `check_outputs.py` reports `SAME`: 101 of 101 files of the XAML compilers
  (generated code, `.xbf`, `.baml`, the WPF resources file), 4 of 4 resource
  indexes by content, and for all 8 native binaries the same exports, imported
  functions and embedded resources.

**What it does not show:** the runner has not been started (it loads module
DLLs by relative path from a product directory this slice does not assemble),
nor has `PowerToys.AlwaysOnTop.exe`; the ColorPicker module DLL is built and
compared, and only the runner would load it. No tests are in the slice.

## Layout

| Path | What it is |
|------|-----------|
| `bazelrc.txt` | → `.bazelrc`. Toolchain pin, the rules_cc default-define undo, runfiles as links. **`BAZEL_VC` is a machine path** |
| `MODULE.bazel` | rules_cc, rules_dotnet, the pinned Windows SDK, vcpkg from PowerToys' own manifest, and `@nuget` from the lock |
| `nuget_lock.json` | what MSBuild resolved: assemblies for C#, headers, libraries and tools for C++, and every package file MSBuild copied beside a program |
| `any2bazel.json` | the differ's configuration, as the two emitters suggested it |
| `bazel/pch.bzl` | MSVC precompiled headers |
| `bazel/cppwinrt.bzl`, `winrt.bzl`, `rc.bzl` | C++/WinRT projections (platform, references, `-component`), WinRT metadata, resource scripts |
| `bazel/layout.bzl` | `program_directory`: a program with everything the reference build put beside it |
| `bazel/dotnet/xaml_winui.bzl`, `xaml_wpf.bzl`, `pri.bzl`, `apphost.bzl` | the XAML compilers, the resource index, the host executable; their launchers are C# programs under `bazel/dotnet/xaml/` and `apphost/` |
| `bazel/dotnet/csc_assembly.bzl`, `nuget_assets.bzl`, ... | as in the other C# examples |
| `src/**/BUILD.bazel` | one per project |
| `src/**/Generated Files/*`, `src/runner/x64/Release/Manifests/app.manifest` | **snapshots**, see below |

## Snapshots

Ten files in the overlay are copies from the reference build, because a
PowerToys-specific build step generates them and the emitter has no rule for
it:

- `src/common/version/Generated Files/version_gen.h`: a target of
  `version.vcxproj` writes it from `src/Version.props`.
- `Generated Files/resource.h` and `<Project>.rc` of the runner, the Hosts
  and ColorPicker modules and AlwaysOnTop: `tools/build/convert-resx-to-rc.ps1` makes them from
  a base file and the translated strings.
- `src/runner/x64/Release/Manifests/app.manifest`: `mt.exe` merges the Windows
  App SDK's activation manifest into the runner's.

The build works with them, and they are frozen: a change to a translated string
does not reach the binary until a rule replaces the copy
(`--generated-map`). `emit_cc.py` prints the list every time.

## Reproducing

Needs Windows with Developer Mode on, Visual Studio with the C++ workload (the
toolset pinned in `bazelrc.txt`), and Bazel 9.

```bash
git clone https://github.com/microsoft/PowerToys && cd PowerToys
git checkout dbe33947e33b3636ed7c4ac34331d03e5a9818b6
cp -r <any2bazel>/examples/powertoys/workspace-ui/. .
mv bazelrc.txt .bazelrc          # then set BAZEL_VC to your Visual Studio

bazel build //src/... //bazel/...
bazel run //src/modules/Hosts/Hosts:PowerToys.Hosts
bazel run //src/modules/colorPicker/ColorPickerUI:PowerToys.ColorPickerUI   # then Win+Shift+C
```

Color Picker has no window of its own: started like this it stays in the
background and listens for its shortcut (a keyboard hook, as when it runs
without the PowerToys runner). End it with
`taskkill /F /IM PowerToys.ColorPickerUI.exe`.

The first build installs vcpkg ports and downloads 82 NuGet packages. On Git
Bash, set `MSYS_NO_PATHCONV=1` so that labels are not rewritten as paths.

The comparisons need the reference: build these projects with MSBuild in
another clone (a solution filter with the programs above and what they
reference, `-bl`), extract, and then

```bash
bazel aquery 'mnemonic("CSharpCompile|CppCompile|CppLink|CppArchive", //...)' \
    --features=-compiler_param_file --include_param_files --output=jsonproto > aquery.json
python3 <any2bazel>/scripts/extract_bazel.py aquery.json . model.bazel.json
python3 <any2bazel>/scripts/diff.py model.msbuild.json model.bazel.json any2bazel.json
python3 <any2bazel>/scripts/check_outputs.py msbuild.report.json . --reference-root <reference clone>
```

`check_outputs.py` reads the emitters' summaries from the workspace; re-run
both emitters into the clone to have them.

## Known gaps

- **WorkspacesLauncherUI is left out.** It references two COM type libraries,
  and MSBuild generates interop assemblies for them from the machine's
  registry. The emitter can only copy those, and binaries do not belong in an
  example. With them copied it builds and opens its window like the others.
- **Three native binaries import some functions from `kernel32.dll` where the
  reference imports them from API-set DLLs.** Same functions, same code at run
  time; Bazel puts static libraries before `linkopts` on the link line,
  MSBuild after. `check_outputs.py` reports it as a note.
- **The intermediate XAML assemblies are compiled with the project's whole
  source list**, MSBuild's `XamlPreCompile` with a shorter one (no assembly
  information). They only feed the XAML compiler's second pass, whose output is
  identical; the differ warns about the extra sources.
- **`PowerToys.FancyZonesEditor.dll.config` is not in the program directory**
  (`not_laid_out`): MSBuild generates it from `app.config`.
- **Debug symbols** (`.pdb`) are not built or laid out.
