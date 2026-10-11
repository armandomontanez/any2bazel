"""A project's resource index (`.pri`), as MSBuild's PRI targets build it.

A WinUI program does not find its strings, its images or even its compiled
XAML by file name: it asks the resource index beside it (`<Program>.pri`).
Without one a window opens to an exception. MSBuild builds the index at the end
of every project that has resources:

  * a library gets `<Library>.pri` with its own string tables and compiled XAML;
  * a program gets one that also merges the indexes of every library it uses,
    NuGet packages' included.

    winui_pri(
        name = "app_pri",
        out = "App.pri",
        index_name = "App",                       # ProjectPriIndexName
        config = ":app_priconfig",                # the reference build's priconfig.xml
        layout = {"Assets/Logo.png": "Assets\\\\Logo.png"},    # file -> its path in the index
        embed = {":app_xaml2/MainWindow.xbf": "MainWindow.xbf"},
        resources = ["Strings/en-us/Resources.resw"],
        pris = ["//lib:lib_pri", "@nuget//some.package/1.0:lib/net8.0/Some.pri"],
        makepri = "@nuget//microsoft.windows.sdk.buildtools/<version>:bin/<sdk>/x64/makepri.exe",
        makepri_files = ["@nuget//microsoft.windows.sdk.buildtools/<version>:files"],
    )

`config` carries what the build decided about qualifiers (default language,
scale, ...); the lists of files in it are replaced by the attributes here.
Compare the result with the reference's by content, not by bytes:
`makepri dump /if <pri>` prints an index as XML.
"""

def _named(mapping):
    files = []
    for target, name in mapping.items():
        found = target[DefaultInfo].files.to_list()
        if len(found) != 1:
            fail("%s must be one file, to be named %s; it is %d files" % (target.label, name, len(found)))
        files.append((found[0], name))
    return files

def _winui_pri_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.out)
    layout = _named(ctx.attr.layout)
    embed = _named(ctx.attr.embed)
    request = ctx.actions.declare_file(ctx.label.name + ".request.json")
    ctx.actions.write(request, json.encode({
        "makepri": ctx.file.makepri.path,
        "config": ctx.file.config.path,
        "index_name": ctx.attr.index_name,
        "out": out.path,
        # Scratch space of the action, beside its output.
        "stage": out.path + ".stage",
        "layout": [{"path": f.path, "name": name} for f, name in layout],
        "embed": [{"path": f.path, "name": name} for f, name in embed],
        "resources": [f.path for f in ctx.files.resources],
        "pris": [f.path for f in ctx.files.pris],
    }))
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [request.path],
        inputs = [request, ctx.file.config] + [f for f, _ in layout + embed] + ctx.files.resources + ctx.files.pris,
        tools = [ctx.file.makepri] + ctx.files.makepri_files,
        outputs = [out],
        mnemonic = "MakePri",
        progress_message = "Indexing resources %{output}",
    )
    return [DefaultInfo(files = depset([out]))]

winui_pri = rule(
    implementation = _winui_pri_impl,
    attrs = {
        "out": attr.string(mandatory = True, doc = "File name of the index, e.g. `App.pri`."),
        "index_name": attr.string(mandatory = True, doc = "Name of the resource map (`ProjectPriIndexName`)."),
        "config": attr.label(allow_single_file = [".xml"], mandatory = True, doc = "priconfig.xml as the reference build generated it."),
        "layout": attr.label_keyed_string_dict(allow_files = True, doc = "Files that are resources as files -> path in the index, as in the project (`Assets\\\\Logo.scale-200.png`)."),
        "embed": attr.label_keyed_string_dict(allow_files = True, doc = "Files stored in the index (compiled XAML) -> path in the index."),
        "resources": attr.label_list(allow_files = [".resw", ".resjson"], doc = "String tables. The language is the name of the directory they are in."),
        "pris": attr.label_list(allow_files = [".pri"], doc = "Indexes to merge: of libraries and packages the project uses."),
        "makepri": attr.label(allow_single_file = True, mandatory = True, doc = "makepri.exe of the Microsoft.Windows.SDK.BuildTools package."),
        "makepri_files": attr.label_list(allow_files = True, doc = "What it loads (mrmsupport.dll)."),
        "_tool": attr.label(
            default = "//bazel/dotnet/xaml:make_pri",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Builds a resource index with makepri.exe from a project's resources and its libraries' indexes.",
)
