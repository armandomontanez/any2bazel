"""WinUI 3 XAML compilation as Bazel actions.

MSBuild compiles a WinUI project in four steps, and each needs the one before:

    1. XAML compiler, pass 1     .xaml -> `<Page>.g.i.cs` (the fields and
                                 InitializeComponent of each code-behind class)
    2. C# compiler               the project with those, into an INTERMEDIATE
                                 assembly (`<IntDir>intermediatexaml\\`)
    3. XAML compiler, pass 2     .xaml + that assembly (for the project's own
                                 types) -> `<Page>.g.cs`, `XamlTypeInfo.g.cs`,
                                 and the compiled markup, `<Page>.xbf`
    4. C# compiler               the project with all generated code

The compiler ships in the Microsoft.WindowsAppSDK.WinUI package as an MSBuild
task and as a program (`tools/net472/XamlCompiler.exe`); `dotnet build` uses the
program, and it writes what the task writes. `winui_xaml_compile` is one run of
it, steps 1 and 3; steps 2 and 4 are ordinary C# targets:

    winui_xaml_compile(
        name = "app_xaml1",
        pages = ["MainWindow.xaml"],
        applications = ["App.xaml"],
        project = "src/app/App.csproj",          # item specs are relative to its directory
        references = [...],                      # what the C# compile references, as files
        not_from_packages = ["Lib.dll"],         # of those, the ones NuGet did not provide
        settings = {"IsPass1": "True", ...},     # the scalar parameters MSBuild passed
        outs = ["app_xaml1/App.g.i.cs", "app_xaml1/App.g.cs", ...],
        compiler = "@nuget//microsoft.windowsappsdk.winui/<version>:tools/net472/XamlCompiler.exe",
        compiler_files = ["@nuget//microsoft.windowsappsdk.winui/<version>:files"],
    )

Which files a pass writes depends on the markup (a page without `x:Class` has no
code), so `outs` lists them: the reference build's log has the list
(`csharp.xaml` in the report). They are written below `<name>/`, in the layout
of the project's intermediate directory.

Pass 2 needs nothing pass 1 wrote, only the assembly compiled in between
(`local_assembly`). The compiler is a .NET Framework program: it runs on the
framework Windows comes with.
"""

def _in_order(files, names):
    """`files` with those named in `names` first, in that order (by file name,
    whatever the case); the rest after them as they came."""
    position = {name.lower(): index for index, name in enumerate(names)}
    listed = sorted(
        [f for f in files if f.basename.lower() in position],
        key = lambda f: position[f.basename.lower()],
    )
    return listed + [f for f in files if f.basename.lower() not in position]

def _winui_xaml_compile_impl(ctx):
    project_dir = ctx.attr.project.rsplit("/", 1)[0] if "/" in ctx.attr.project else ""
    prefix = ctx.label.name + "/"
    outs = []
    for out in ctx.outputs.outs:
        relative = out.short_path[len(ctx.label.package) + 1:] if ctx.label.package else out.short_path
        if not relative.startswith(prefix):
            fail("outs must be below %s, got %s" % (prefix, relative))
        outs.append(out)
    if not outs:
        fail("outs is empty: the pass would have nothing to produce")
    output_dir = outs[0].path[:outs[0].path.index("/" + prefix) + len(prefix)]

    def page(f):
        if project_dir and not f.path.startswith(project_dir + "/"):
            fail("%s is not below the project directory %s; a linked page is not supported yet" % (f.path, project_dir))
        return {
            "path": f.path,
            "spec": (f.path[len(project_dir) + 1:] if project_dir else f.path).replace("/", "\\"),
        }

    compiler = ctx.file.compiler
    request = ctx.actions.declare_file(ctx.label.name + ".request.json")
    ctx.actions.write(request, json.encode({
        "compiler": compiler.path,
        # <package>/tools/net472/XamlCompiler.exe -> <package>/tools, which has
        # <architecture>/GenXbf.dll, the writer of compiled markup.
        "gen_xbf": compiler.dirname.rsplit("/", 1)[0],
        "project": ctx.attr.project,
        "output": output_dir,
        "settings": ctx.attr.settings,
        "pages": [page(f) for f in ctx.files.pages],
        "applications": [page(f) for f in ctx.files.applications],
        "references": [f.path for f in _in_order(ctx.files.references, ctx.attr.reference_order)],
        "not_from_packages": ctx.attr.not_from_packages,
        "local_assembly": ctx.file.local_assembly.path if ctx.file.local_assembly else "",
        "outs": [f.path for f in outs],
    }))
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [request.path],
        inputs = depset(
            [request] + ctx.files.pages + ctx.files.applications + ctx.files.references +
            ([ctx.file.local_assembly] if ctx.file.local_assembly else []),
        ),
        tools = [compiler] + ctx.files.compiler_files,
        outputs = outs,
        mnemonic = "XamlCompile",
        progress_message = "Compiling XAML %{label}",
    )
    return [DefaultInfo(files = depset(outs))]

winui_xaml_compile = rule(
    implementation = _winui_xaml_compile_impl,
    attrs = {
        "pages": attr.label_list(allow_files = [".xaml"], doc = "The project's `Page` items."),
        "applications": attr.label_list(allow_files = [".xaml"], doc = "Its `ApplicationDefinition` items."),
        "project": attr.string(mandatory = True, doc = "Workspace path of the project file. Only its location matters: pages are named relative to its directory."),
        "references": attr.label_list(allow_files = True, doc = "The assemblies and metadata the project's C# compile references."),
        "reference_order": attr.string_list(doc = "File names of the references in the order MSBuild passed them. The compiler numbers types as it meets them, so the order shows in `XamlTypeInfo.g.cs`."),
        "not_from_packages": attr.string_list(doc = "File names of references that are not a NuGet package's (another project's assembly, a file referenced by path)."),
        "local_assembly": attr.label(allow_single_file = [".dll"], doc = "Pass 2: the intermediate assembly."),
        "settings": attr.string_dict(doc = "The compiler's scalar parameters as MSBuild passed them (`IsPass1`, `RootNamespace`, `FeatureControlFlags`, ...)."),
        "outs": attr.output_list(mandatory = True, doc = "The files this pass writes, as `<name>/<path below the intermediate directory>`."),
        "compiler": attr.label(allow_single_file = True, mandatory = True, doc = "XamlCompiler.exe of the Microsoft.WindowsAppSDK.WinUI package."),
        "compiler_files": attr.label_list(allow_files = True, doc = "What it loads: the other files of `tools/net472` and `tools/<architecture>/GenXbf.dll`."),
        "_tool": attr.label(
            default = "//bazel/dotnet/xaml:xaml_compile",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "One pass of the WinUI XAML compiler over a project's markup.",
)
