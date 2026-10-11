# Reference build: MSBuild, XAML (WinUI 3 and WPF)

A project with `.xaml` files is a C# project with more steps around its compile:
a markup compiler that writes code, a second compile, and for WinUI a resource
index the program cannot start without. This guide covers those steps and the
directory a program runs from. Everything else (the report, the lock, how C# is
compared) is in [REFERENCE-msbuild-csharp.md](REFERENCE-msbuild-csharp.md).
When a step fails, look its message up in the
[playbook](REFERENCE-msbuild-playbook.md#symptom-index) first.

```bash
# After the usual extraction and lock, with the templates in the workspace:
python3 "$SKILL_DIR/scripts/emit_cc.py"     msbuild.report.json nuget_lock.json <workspace> --reference-root <ref>
python3 "$SKILL_DIR/scripts/emit_dotnet.py" msbuild.report.json nuget_lock.json <workspace> --reference-root <ref>
bazel build //...
python3 "$SKILL_DIR/scripts/check_outputs.py" msbuild.report.json <workspace> --reference-root <ref>
bazel run //src/app:Contoso.App
```

Run `emit_cc.py` first when the solution has C++: a UI program takes native
DLLs along, and `emit_dotnet.py` reads the C++ emitter's summary to find them.

## What the report records

`extract_msbuild.py` needs no extra option. A XAML project gets, under
`targets.<name>.csharp`:

| key | content |
|---|---|
| `xaml.framework` | `winui` or `wpf` |
| `xaml.pass1`, `xaml.pass2` | per run of the markup compiler: pages and application definitions (path and item spec), the scalar parameters MSBuild passed, and the files it wrote |
| `xaml.pass1.reference_order`, `references_not_from_packages` | WinUI: the compiler's references by file name in the order given, and which of them are not a NuGet package's |
| `xaml.precompile` | WinUI: the digest of the compile in between (target `XamlPreCompile`) |
| `xaml.pass1.requires_pass2`, `state`, `xaml.resources` | WPF: whether any markup needs the second pass, the files pass 1 leaves for it, and what goes into `<Assembly>.g.resources` |
| `apphost` | a program: the template of its host executable, the assembly it starts, whether it is a GUI program |

and under `targets.<name>.pri` the resource index: output, index name, the
configuration file, and its inputs by kind.

A WPF project appears twice in the log: MSBuild generates a temporary project
(`<Project>_<random>_wpftmp`) to compile the intermediate assembly. The emitter
lists it as excluded; its compile is the `_intermediate` target of the project
it belongs to. A generator both would run (cswinrt) runs in the temporary
project and is up to date by the time the real one gets to it: the emitter
takes such steps from there.

These digests come from tasks that ran. A XAML project that was up to date in
the log (an incremental build) has none of them, and the emitter then excludes
it: take such a project from the log of the build that built it.

## WinUI 3

MSBuild compiles a WinUI project in four steps, each needing the one before:

1. **XAML compiler, pass 1**: `.xaml` to `<Page>.g.i.cs` (the fields and
   `InitializeComponent` of each code-behind class).
2. **C# compiler**: the project with that code, into an intermediate assembly
   (`<IntDir>intermediatexaml\`).
3. **XAML compiler, pass 2**: reads that assembly for the project's own types
   and writes `<Page>.g.cs`, `XamlTypeInfo.g.cs` and the compiled markup,
   `<Page>.xbf`.
4. **C# compiler**: the project with all generated code.

The compiler ships in the Microsoft.WindowsAppSDK.WinUI package twice: as an
MSBuild task, which Visual Studio's MSBuild runs in process, and as a program
(`tools/net472/XamlCompiler.exe <input.json> <output.json>`), which
`dotnet build` uses. `xaml_winui.bzl` runs the program, through a launcher
(`xaml/XamlCompile.cs`) that writes its input, runs it from the project
directory and prints its diagnostics; the program's exit code says nothing
about them.

```starlark
winui_xaml_compile(
    name = "Hosts_xaml1",
    applications = ["HostsXAML/App.xaml"],
    pages = ["HostsXAML/MainWindow.xaml"],
    project = "src/modules/Hosts/Hosts/Hosts.csproj",
    references = [...],                  # what the C# compile references, as files
    reference_order = [...],             # their file names, in MSBuild's order
    not_from_packages = ["PowerToys.HostsUILib.dll", ...],
    settings = {"IsPass1": "True", "RootNamespace": "Hosts", ...},
    outs = ["Hosts_xaml1/HostsXAML/App.g.i.cs", ...],
    compiler = "@nuget//microsoft.windowsappsdk.winui/2.2.1:tools/net472/XamlCompiler.exe",
    compiler_files = ["@nuget//microsoft.windowsappsdk.winui/2.2.1:files"],
)
csc_assembly(name = "Hosts_intermediate", ...)                 # step 2
winui_xaml_compile(name = "Hosts_xaml2", local_assembly = ":Hosts_intermediate", ...)
exact_csharp_assembly(name = "Hosts", srcs = [..., ":Hosts_xaml1/HostsXAML/App.g.i.cs",
                                              ":Hosts_xaml2/HostsXAML/App.g.cs", ...])
```

What was established by running the program by hand against the reference
build's own output, and then under Bazel:

- **It writes what the task writes.** Generated code, `XamlTypeInfo.g.cs`, the
  processed `.xaml` and the `.xbf` are byte-identical, with one exception:
  `#pragma checksum "<absolute path of the .xaml>"` at the top of generated
  code. The launcher rewrites it to the path from the execution root, so the
  file is the same on every machine; `check_outputs.py` ignores that line.
- **Pass 2 needs nothing pass 1 wrote**, only the intermediate assembly. The two
  passes are independent actions.
- **Which files a pass writes depends on the markup.** A page without `x:Class`
  (a resource dictionary, a control template) has no code. The reference log
  has the list; `outs` restates it.
- **The order of references shows.** The compiler numbers types as it meets
  them, so `XamlTypeInfo.g.cs` of a program lists them in the order of its
  references. Sorted, the table comes out permuted (same types, another file).
- **Null and empty are different inputs.** A list MSBuild did not pass must be
  `null` in the JSON (an empty `LocalAssembly` makes pass 1 index into it),
  except `ReferenceAssemblyPaths`, which the compiler walks unchecked.
- **The machine-specific parameters are not needed for C#**: the Visual C++
  directories, the Windows SDK path and `vcmeta.dll` can all be left out and
  the output does not change. `GenXbfPath` is needed (the writer of `.xbf`, in
  the same package). The compiler is a .NET Framework program: it runs on the
  framework Windows comes with, the one input the action does not declare.

Both compiles go through `csc_assembly` (the exact-command-line template): the
XAML compiler takes the compile's references as files, which a rules_dotnet
target does not expose.

## The resource index

A WinUI program does not find its strings, its images or even its compiled XAML
by file name: it asks the resource index beside it, `<Program>.pri`. Without
one, a window opens to an exception. MSBuild builds it at the end of every
project that has resources: a library's holds its string tables (`.resw`) and
its `.xbf`; a program's also merges the index of every library and package it
uses. (46 of PowerToys' C# projects without any XAML have one, for their
strings.)

`pri.bzl` runs `makepri.exe` of the Microsoft.Windows.SDK.BuildTools package
through a launcher (`xaml/MakePri.cs`), with the reference build's
`priconfig.xml` (restated by the emitter: it carries the default language and
qualifiers) and the four kinds of input as attributes:

```starlark
winui_pri(
    name = "Hosts_pri",
    out = "PowerToys.Hosts.pri",
    index_name = "PowerToys.Hosts",
    config = ":Hosts_priconfig",
    layout = {"Assets/Hosts/AppList.scale-100.png": "Assets\\Hosts\\AppList.scale-100.png", ...},
    embed = {":Hosts_xaml2/HostsXAML/App.xbf": "HostsXAML\\App.xbf", ...},
    pris = ["//src/modules/Hosts/HostsUILib:HostsUILib_pri", "@nuget//...:lib/.../X.pri", ...],
    makepri = "@nuget//microsoft.windows.sdk.buildtools/10.0.26100.6901:bin/10.0.26100.0/x64/makepri.exe",
)
```

makepri names a resource after its path below the index root, so the launcher
lays the inputs out under one staging directory the way the project's were. A
library's compiled XAML is embedded under `<Library>\`; MSBuild sometimes embeds
a copy it made in the output directory, and the emitter finds the file by name.

Compare indexes by content: `check_outputs.py` dumps both (`makepri dump`) and
compares every named resource with its candidates.

## WPF

WPF's markup compiler has no command line at all. It is three MSBuild task
classes in `PresentationBuildTasks.dll`, which the .NET SDK ships
(`Sdks/Microsoft.NET.Sdk.WindowsDesktop/tools/`):

1. **MarkupCompilePass1**: `.xaml` to `<Page>.g.cs` for every page, and
   `<Page>.baml` (compiled markup) for pages that name no type of the project.
2. **C# compiler**: the project with that code, into a temporary assembly.
3. **MarkupCompilePass2**: the remaining `.baml`, with the project's types
   looked up in that assembly. Steps 2 and 3 only happen when some markup needs
   them (`requires_pass2`).
4. **ResourcesGenerator**: every `.baml`, and the project's `Resource` items
   (images the markup shows), into `<Assembly>.g.resources`.
5. **C# compiler**: the project with the generated code, embedding that file.

`xaml_wpf.bzl` runs the tasks of the toolchain's own SDK in a small host
(`xaml/WpfMarkup.cs`): it creates the task, sets the properties MSBuild set,
hands it a build engine that prints diagnostics, and executes it. Nothing in
the host is compiled against MSBuild.

```starlark
wpf_markup_compile(name = "Editor_wpf1", pass_ = 1, pages = [...], applications = ["App.xaml"],
                   sources = [...], references = [...], settings = {...}, outs = [...])
csc_assembly(name = "Editor_intermediate", ...)
wpf_markup_compile(name = "Editor_wpf2", pass_ = 2, local_assembly = ":Editor_intermediate",
                   state = [":Editor_wpf1/X_MarkupCompile.cache", ":Editor_wpf1/X_MarkupCompile.lref"], ...)
wpf_resources(name = "Editor_markup", out = "X.g.resources", files = {...})
exact_csharp_assembly(name = "Editor", resources = {":Editor_markup": "X.g.resources"}, ...)
```

What was established:

- **Hosted outside MSBuild, the tasks write the same files**: every `.baml` and
  the `.g.resources` are byte-identical to the reference's.
- **Generated code differs in three kinds of lines**, none of them code:
  `#pragma checksum` and `#line` name the markup by its path from the output
  directory, which is another path under Bazel; `GeneratedCodeAttribute` has
  the tool's version, and the toolchain's SDK is not the reference's (10.0.11
  against 10.0.12 here); and only the .NET Framework flavour of the tasks,
  which Visual Studio's MSBuild runs, writes a `Runtime Version` comment.
  `check_outputs.py` leaves these lines out.
- **The task remembers the directory it was CREATED in** as the project's.
  Markup is named by its path from there, and that name is in the program
  (`/Assembly;component/views/main.xaml`): the host changes directory first.
- **Pass 2 continues from files pass 1 wrote** (`<Assembly>_MarkupCompile.cache`
  and `.lref`) and updates them. They are outputs of the first action and
  copied, writable, into the second's directory.
- **`Content` items are recorded, not read**: the generated code lists their
  paths as the project spells them.

## Programs

A project compiled through `csc_assembly` produces a `.dll`. To start it, it
needs what MSBuild puts around it:

- **The host executable.** `<Program>.exe` is a copy of `apphost.exe` from the
  Microsoft.NETCore.App.Host pack with the assembly's name written into it, the
  GUI subsystem set and the assembly's icon and version resources copied over.
  `apphost.bzl` calls the same method MSBuild's task calls
  (`HostWriter.CreateAppHost` of the SDK's Microsoft.NET.HostModel.dll). The
  template is locked like any pack file.
- **`<Program>.runtimeconfig.json` and `.deps.json`**, restated from the
  reference build.
- **Everything MSBuild copied beside it.** A self-contained WinUI program has
  the .NET runtime, the Windows App SDK, its libraries, their satellite
  resources and its native DLLs in its directory: 892 copies for PowerToys'
  Hosts. The emitter maps each copied file to whatever provides it in the
  Bazel build: a package file (the lock lists them under `deployed`), another
  project's assembly, a native target of `emit_cc.py`, a resource index,
  compiled XAML of a library, a source file.
- **Including what was there already.** PowerToys builds every WinUI program
  into one `WinUI3Apps` directory. MSBuild's copy-if-newer target
  (`_CopyOutOfDateSourceItemsToOutputDirectory`) hands its Copy task only the
  files that are not there yet, so the second program's log has 2 copies where
  the first had 291, in a clean build as well, and the Windows App SDK runtime
  is not among them. The extractor completes the list from the items the
  target was given: a `copy` step marked `up_to_date`. Without it the second
  program's directory lacks `Microsoft.WindowsAppRuntime.dll`, and the program
  ends at startup with `DllNotFoundException`.

```starlark
dotnet_apphost(name = "Hosts_host", out = "PowerToys.Hosts.exe", assembly = ":Hosts_csc", gui = True,
               template = "@nuget//microsoft.netcore.app.host.win-x64/10.0.12:runtimes/win-x64/native/apphost.exe")
program_directory(
    name = "PowerToys.Hosts",
    executable = "PowerToys.Hosts.exe",
    files = {"PowerToys.Hosts.exe": ":Hosts_host", "PowerToys.Hosts.dll": ":Hosts_csc",
             "PowerToys.Hosts.pri": ":Hosts_pri", "PowerToys.Interop.dll": "//src/common/interop:PowerToys.Interop.dll",
             "Microsoft.WindowsAppRuntime.dll": "@nuget//...", ...},
)
```

`bazel run //src/modules/Hosts/Hosts:PowerToys.Hosts` starts the program from
that directory (`layout.bzl`; the files are symbolic links where Bazel may make
them). A copied file with no provider is listed under `not_laid_out` in the
summary, not guessed at; debug symbols and documentation files of the build's
own targets are left out and counted.

## What the emitter cannot generate

- **`<COMReference>`**: MSBuild generates an interop assembly
  (`Interop.Shell32.dll`) from a type library registered on the machine. The
  emitter copies the reference build's file into the workspace and says so
  (`snapshot`). A rule would run `tlbimp` over the system's type library.
- **Files a project-specific step writes**, as for C++ (snapshots, or
  `--generated-map`).
- **A page outside the project directory** (a linked `Page` item): the rule
  refuses it.

## `check_outputs.py`

```
xaml: 5 compared, 0 differ
  same Hosts (9 files)
  ...
pri: 3 compared, 0 differ
  same Hosts (338 resources)
binaries: 7 compared, 0 differ
  same runner (0 exports, 62 imports)
  same HostsModuleInterface (1 exports, 8 imports)
         note: 13 functions are imported from another DLL than in the reference ...
SAME
```

The diff compares command lines; these outputs come from tools whose command
line says little about the result. Run it after building, with the workspace
the emitters wrote into. A program that opens its window is the other check
that counts: start the reference build's program and the Bazel one the same
way and compare what appears.

## Field notes

- A helper that copies a Bazel output copies its read-only attribute. A tool
  that updates the copy, or a second run that replaces it, fails with "access
  denied". The launchers clear the attribute.
- Output files of a rule inherit its visibility: a program in another package
  that lays a library's compiled XAML out needs the XAML targets public.
- With a rules_dotnet tool on Windows, `--enable_runfiles` is required, and
  `startup --windows_enable_symlinks` (with Developer Mode) keeps each tool's
  runfiles tree, which holds the whole .NET SDK, from being a copy.

## What has been exercised

A 33-project slice of PowerToys at one commit (Release|x64, Visual Studio 18,
.NET SDK 10.0.401 for the reference, 10.0.400 under Bazel), built from a clean
clone as the reference (31 projects, then ColorPicker's two by an incremental
build) and from another clone that never held MSBuild output:

| | |
|---|---|
| WinUI 3 | 4 projects (two programs, two libraries; 18 `.xaml`): 64 of 64 compiler outputs identical, 4 of 4 resource indexes with the same content |
| WPF | 2 programs (24 `.xaml`): 45 of 45 outputs identical (22 `.baml`, 2 resources files, 21 generated sources) |
| programs | `PowerToys.Hosts.exe` opens its "Hosts File Editor" window from `bazel run`; `PowerToys.WorkspacesLauncherUI.exe` opens its window; `PowerToys.FancyZonesEditor.exe` shows the same "Editor data parsing error" dialog as the reference build's when started without FancyZones; `PowerToys.ColorPickerUI.exe` (the second WinUI program in `WinUI3Apps`, with Win2D) starts from `bazel run`, waits for its shortcut, and shows its picker overlay when the runner's event is signalled |
| the slice | 17 C++ and 16 C# projects; `diff.py` converges with 0 errors |

Not exercised: the other 20 WinUI projects of PowerToys, Settings (101 `.xaml`)
among them; C++ XAML; linked pages; packaged (MSIX) programs and package
identity; satellite assemblies of localized markup
(`RequirePass2ForSatelliteAssembly`); ahead-of-time compilation; ARM64.
