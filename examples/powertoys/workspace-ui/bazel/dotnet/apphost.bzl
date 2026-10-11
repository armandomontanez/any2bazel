"""The host executable of a .NET program (`<Program>.exe`).

MSBuild's `_CreateAppHost` target copies `apphost.exe` out of the
Microsoft.NETCore.App.Host pack and writes the program's `.dll` name into it;
for a Windows program it also sets the GUI subsystem and copies the assembly's
icon and version information. rules_dotnet does the equivalent inside
`csharp_binary`. An assembly compiled by `csc_assembly` (csc_assembly.bzl) has
no host, and so nothing to start: this rule makes one.

    dotnet_apphost(
        name = "App_host",
        out = "App.exe",
        assembly = ":App_csc",            # for its name and its resources
        gui = True,                       # <OutputType>WinExe</OutputType>
        template = "@nuget//microsoft.netcore.app.host.win-x64/<version>:runtimes/win-x64/native/apphost.exe",
    )

The host only works beside the program's assembly, its `.runtimeconfig.json`
and `.deps.json`, and whatever those name: lay them out with `layout.bzl`.
"""

def _dotnet_apphost_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.out)
    assembly = ctx.file.assembly
    args = ctx.actions.args()
    args.add(ctx.file._host_model)
    args.add(ctx.file.template)
    args.add(out)
    args.add(assembly.basename)
    args.add("gui" if ctx.attr.gui else "console")
    args.add(assembly)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = [ctx.file.template, assembly, ctx.file._host_model],
        outputs = [out],
        mnemonic = "DotnetAppHost",
        progress_message = "Writing program host %{output}",
    )
    return [DefaultInfo(files = depset([out]))]

dotnet_apphost = rule(
    implementation = _dotnet_apphost_impl,
    attrs = {
        "out": attr.string(mandatory = True, doc = "File name of the host, e.g. `App.exe`."),
        "assembly": attr.label(allow_single_file = [".dll"], mandatory = True, doc = "The program's assembly: the host starts the file of this name beside it, and takes its resources."),
        "gui": attr.bool(default = False, doc = "A program for the Windows subsystem (no console)."),
        "template": attr.label(allow_single_file = True, mandatory = True, doc = "`apphost.exe` of the Microsoft.NETCore.App.Host pack the reference build used."),
        "_host_model": attr.label(default = "//bazel/dotnet/apphost:host_model", allow_single_file = True),
        "_tool": attr.label(default = "//bazel/dotnet/apphost", executable = True, cfg = "exec"),
    },
    doc = "Writes the executable that starts a .NET program's assembly.",
)
