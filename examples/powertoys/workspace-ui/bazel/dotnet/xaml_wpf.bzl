"""WPF XAML compilation as Bazel actions.

MSBuild compiles a WPF project's markup around its C# compile:

    1. MarkupCompilePass1   .xaml -> `<Page>.g.cs` for every page, and
                            `<Page>.baml` (the compiled markup) for pages that
                            name no type of the project itself
    2. C# compiler          the project with the generated code, into a
                            TEMPORARY assembly (a project of its own under
                            MSBuild, `<Project>_<random>_wpftmp.csproj`)
    3. MarkupCompilePass2   the remaining `.baml`, now that the project's types
                            can be looked up in that assembly
    4. ResourcesGenerator   every `.baml` -> `<Assembly>.g.resources`
    5. C# compiler          the project with the generated code, embedding the
                            resources file

Steps 2 and 3 only happen when some markup needs them (`requires_pass2` in the
report). The markup compiler is not a program: it is MSBuild task classes in
PresentationBuildTasks.dll of the .NET SDK, here the toolchain's own, run by a
small host (xaml/WpfMarkup.cs).

    wpf_markup_compile(
        name = "app_wpf1",
        pass_ = 1,
        pages = ["MainWindow.xaml"],
        applications = ["App.xaml"],
        project_dir = "src/app",
        sources = [...],                 # the project's C# sources
        references = [...],              # what its compile references, as files
        settings = {"AssemblyName": "App", "OutputType": "WinExe", ...},
        outs = ["app_wpf1/App.g.cs", "app_wpf1/App_MarkupCompile.cache", ...],
    )
    wpf_markup_compile(
        name = "app_wpf2",
        pass_ = 2,
        project_dir = "src/app",
        local_assembly = ":app_intermediate",
        state = [":app_wpf1/App_MarkupCompile.cache", ":app_wpf1/App_MarkupCompile.lref"],
        references = [...], settings = {...},
        outs = ["app_wpf2/MainWindow.baml", "app_wpf2/App.baml"],
    )
    wpf_resources(
        name = "app_markup",
        out = "App.g.resources",
        files = {":app_wpf1/Other.baml": "Other.baml", ":app_wpf2/MainWindow.baml": "MainWindow.baml", ...},
    )

Generated code names its markup by the path from the output directory
(`#pragma checksum`, `#line`), which is another path under Bazel; nothing else
in it depends on where it was generated.
"""

def _tasks(ctx):
    return ctx.file._tasks

def _run(ctx, request, inputs, outs, what):
    file = ctx.actions.declare_file(ctx.label.name + ".request.json")
    ctx.actions.write(file, json.encode(request))
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [file.path],
        inputs = depset([file] + inputs),
        tools = [_tasks(ctx)] + ctx.files._sdk,
        outputs = outs,
        mnemonic = "WpfMarkup",
        progress_message = what + " %{label}",
    )

def _output_dir(ctx, outs):
    prefix = ctx.label.name + "/"
    for out in outs:
        relative = out.short_path[len(ctx.label.package) + 1:] if ctx.label.package else out.short_path
        if not relative.startswith(prefix):
            fail("outs must be below %s, got %s" % (prefix, relative))
    if not outs:
        fail("outs is empty")
    return outs[0].path[:outs[0].path.index("/" + prefix) + len(prefix)]

def _wpf_markup_compile_impl(ctx):
    outs = ctx.outputs.outs
    project_dir = ctx.attr.project_dir

    def named(f):
        if project_dir and not f.path.startswith(project_dir + "/"):
            fail("%s is not below the project directory %s" % (f.path, project_dir))
        return {"path": f.path, "spec": (f.path[len(project_dir) + 1:] if project_dir else f.path).replace("/", "\\")}

    first = ctx.attr.pass_ == 1
    request = {
        "tasks": _tasks(ctx).path,
        "mode": "pass1" if first else "pass2",
        "project_dir": project_dir,
        "output": _output_dir(ctx, outs),
        "settings": ctx.attr.settings,
        "references": [f.path for f in ctx.files.references],
        "outs": [f.path for f in outs],
    }
    inputs = list(ctx.files.references)
    if first:
        request["pages"] = [named(f) for f in ctx.files.pages]
        request["applications"] = [named(f) for f in ctx.files.applications]
        request["content"] = ctx.attr.content
        request["sources"] = [f.path for f in ctx.files.sources]
        inputs += ctx.files.pages + ctx.files.applications + ctx.files.sources
    else:
        if not ctx.file.local_assembly:
            fail("pass 2 needs local_assembly")
        request["state"] = [f.path for f in ctx.files.state]
        request["local_assembly"] = ctx.file.local_assembly.path

        # Pass 2 opens the markup pass 1 left for it.
        inputs += ctx.files.state + [ctx.file.local_assembly] + ctx.files.pages + ctx.files.applications
    _run(ctx, request, inputs, outs, "Compiling WPF markup")
    return [DefaultInfo(files = depset(outs))]

def _wpf_resources_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.out)
    files = []
    for target, name in ctx.attr.files.items():
        found = target[DefaultInfo].files.to_list()
        if len(found) != 1:
            fail("%s must be one file" % target.label)
        files.append((found[0], name))
    _run(ctx, {
        "tasks": _tasks(ctx).path,
        "mode": "resources",
        "project_dir": ctx.label.package,
        "output": out.path + ".stage",
        "settings": {},
        "resources": [{"path": f.path, "name": name} for f, name in files],
        "out": out.path,
        "outs": [out.path],
    }, [f for f, _ in files], [out], "Packing WPF markup")
    return [DefaultInfo(files = depset([out]))]

_COMMON = {
    "_tasks": attr.label(default = "//bazel/dotnet/xaml:presentation_build_tasks", allow_single_file = True),
    "_sdk": attr.label(default = "//bazel/dotnet/xaml:presentation_build_tasks_needs", allow_files = True),
    "_tool": attr.label(default = "//bazel/dotnet/xaml:wpf_markup", executable = True, cfg = "exec"),
}

wpf_markup_compile = rule(
    implementation = _wpf_markup_compile_impl,
    attrs = dict(_COMMON, **{
        "pass_": attr.int(mandatory = True, values = [1, 2]),
        "pages": attr.label_list(allow_files = [".xaml"], doc = "The project's `Page` items."),
        "applications": attr.label_list(allow_files = [".xaml"], doc = "Its `ApplicationDefinition`."),
        "content": attr.string_list(doc = "Pass 1: the project's `Content` items, as their paths from the project directory. The generated code lists them; the files are not read."),
        "sources": attr.label_list(allow_files = True, doc = "Pass 1: the project's C# sources (it looks the code-behind classes up in them)."),
        "project_dir": attr.string(mandatory = True, doc = "Workspace path of the project directory: markup is named relative to it."),
        "references": attr.label_list(allow_files = True, doc = "What the project's C# compile references."),
        "local_assembly": attr.label(allow_single_file = [".dll"], doc = "Pass 2: the temporary assembly."),
        "state": attr.label_list(allow_files = True, doc = "Pass 2: `<Assembly>_MarkupCompile.cache` and `.lref` of pass 1."),
        "settings": attr.string_dict(doc = "The task's scalar parameters as MSBuild passed them."),
        "outs": attr.output_list(mandatory = True, doc = "The files the pass writes, as `<name>/<path below the intermediate directory>`."),
    }),
    doc = "One pass of the WPF markup compiler over a project's markup.",
)

wpf_resources = rule(
    implementation = _wpf_resources_impl,
    attrs = dict(_COMMON, **{
        "out": attr.string(mandatory = True, doc = "`<Assembly>.g.resources`."),
        "files": attr.label_keyed_string_dict(allow_files = True, doc = "Compiled markup -> its path below the intermediate directory, which is its name in the program."),
    }),
    doc = "Packs a project's compiled markup into the resources file its assembly embeds.",
)
