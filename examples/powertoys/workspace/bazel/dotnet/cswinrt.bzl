"""C#/WinRT projection sources as a Bazel action.

A C# project that uses a WinRT component does not reference its `.winmd`
directly (on modern .NET it cannot). The CsWinRT package's MSBuild targets run
`cswinrt.exe` before every compile and add the C# it writes to the project's
sources. The command line is the project's
`obj/.../Generated Files/CsWinRT/cswinrt.rsp`:

    -target net8.0
    -input 10.0.26100.0                  the Windows SDK, by version
    -input <component>.winmd
    -output "...\\Generated Files\\CsWinRT"
    -include <namespace> ... -exclude <namespace> ...

`-input <SDK version>` makes the tool look the SDK up in the registry. This rule
passes the SDK's metadata files instead, from the pinned SDK repository, so the
action reads nothing it does not declare.

The output is a directory of `.cs` files whose name ends in `.cs`, which is what
lets it be listed in `srcs` of a rules_dotnet target.
"""

def _cswinrt_projection_impl(ctx):
    out = ctx.actions.declare_directory(ctx.attr.out or ctx.label.name + ".cs")
    args = ctx.actions.args()
    args.add("-target", ctx.attr.target)
    for winmd in ctx.files.sdk_winmds + ctx.files.winmds:
        args.add("-input", winmd)
    args.add("-output", out.path)
    for namespace in ctx.attr.excludes:
        args.add("-exclude", namespace)
    for namespace in ctx.attr.includes:
        args.add("-include", namespace)
    args.add_all(ctx.attr.opts)
    args.use_param_file("@%s", use_always = True)
    args.set_param_file_format("multiline")
    ctx.actions.run(
        executable = ctx.executable.cswinrt,
        arguments = [args],
        inputs = ctx.files.sdk_winmds + ctx.files.winmds,
        outputs = [out],
        mnemonic = "CsWinRT",
        progress_message = "Generating C#/WinRT projection %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

cswinrt_projection = rule(
    implementation = _cswinrt_projection_impl,
    attrs = {
        "winmds": attr.label_list(allow_files = [".winmd"], doc = "Component metadata to project."),
        "sdk_winmds": attr.label_list(
            allow_files = [".winmd"],
            doc = "Windows SDK metadata, where the reference passes `-input <SDK version>`.",
        ),
        "out": attr.string(doc = "Name of the output directory; must end in `.cs`. Defaults to `<name>.cs`. Keep it short: cswinrt.exe writes nothing, without an error, to a path longer than MAX_PATH."),
        "target": attr.string(mandatory = True, doc = "`-target`, e.g. `net8.0`."),
        "includes": attr.string_list(doc = "`-include` namespaces or types, in the reference's order."),
        "excludes": attr.string_list(doc = "`-exclude` namespaces or types."),
        "opts": attr.string_list(doc = "Any other cswinrt options of the reference command line."),
        "cswinrt": attr.label(
            executable = True,
            cfg = "exec",
            allow_single_file = True,
            mandatory = True,
            doc = "cswinrt.exe from the Microsoft.Windows.CsWinRT package.",
        ),
    },
    doc = "Runs cswinrt.exe; list the result in `srcs` of the projection assembly.",
)
