"""C++/WinRT projection headers as a Bazel action.

In MSBuild, importing the Microsoft.Windows.CppWinRT package makes EVERY
project run `cppwinrt.exe` over the Windows SDK's contract metadata and write
the projection headers (`winrt/*.h`) into its own intermediate directory
(`<IntDir>Generated Files`). The command line is the project's
`*.vcxproj.cppwinrt_plat.rsp`:

    -in <every contract .winmd of the SDK version> -out "<IntDir>Generated Files\\."

The output depends only on the tool and the SDK metadata, so one action serves
every target that needs it.
"""

load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

def _cppwinrt_projection_impl(ctx):
    out = ctx.actions.declare_directory(ctx.label.name)
    args = ctx.actions.args()
    for winmd in ctx.files.winmds:
        args.add("-in", winmd)
    for winmd in ctx.files.references:
        args.add("-ref", winmd)
    args.add("-out", out.path)
    ctx.actions.run(
        executable = ctx.executable.cppwinrt,
        arguments = [args],
        inputs = ctx.files.winmds + ctx.files.references,
        outputs = [out],
        mnemonic = "CppWinRT",
        progress_message = "Generating C++/WinRT projection %{label}",
    )
    return [
        DefaultInfo(files = depset([out])),
        CcInfo(compilation_context = cc_common.create_compilation_context(
            headers = depset([out]),
            includes = depset([out.path]),
        )),
    ]

cppwinrt_projection = rule(
    implementation = _cppwinrt_projection_impl,
    attrs = {
        "winmds": attr.label_list(
            allow_files = [".winmd"],
            mandatory = True,
            doc = "Metadata to project (`-in`).",
        ),
        "references": attr.label_list(
            allow_files = [".winmd"],
            doc = "Metadata to resolve against without projecting (`-ref`).",
        ),
        "cppwinrt": attr.label(
            executable = True,
            cfg = "exec",
            allow_single_file = True,
            mandatory = True,
            doc = "cppwinrt.exe, from the Microsoft.Windows.CppWinRT NuGet package.",
        ),
    },
    doc = "Runs cppwinrt.exe; depend on the result like a header-only cc_library.",
)
