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

def _cppwinrt_component_impl(ctx):
    out = ctx.actions.declare_directory(ctx.label.name)
    args = ctx.actions.args()
    args.add_all(ctx.attr.opts)

    # Implementation skeletons, which the reference also writes and no compile
    # reads; inside the output so the action declares everything it writes.
    args.add("-comp", out.path + "/sources")
    for winmd in ctx.files.winmds:
        args.add("-in", winmd)
    for winmd in ctx.files.references:
        args.add("-ref", winmd)
    args.add("-out", out.path)
    args.use_param_file("@%s", use_always = True)
    args.set_param_file_format("multiline")
    ctx.actions.run(
        executable = ctx.executable.cppwinrt,
        arguments = [args],
        inputs = ctx.files.winmds + ctx.files.references,
        outputs = [out],
        mnemonic = "CppWinRT",
        progress_message = "Generating C++/WinRT component projection %{label}",
    )

    # module.g.cpp is the one generated file that is compiled on its own (the
    # `<Class>.g.cpp` files are included by the hand-written sources). A source
    # has to be a file, not a member of a directory output: copy it out.
    module = ctx.actions.declare_file(ctx.label.name + ".sources/module.g.cpp")
    script = ctx.actions.declare_file(ctx.label.name + ".copy_module.bat")
    ctx.actions.write(
        script,
        "@copy /Y \"%s\\module.g.cpp\" \"%s\" >NUL\r\n" % (out.path.replace("/", "\\"), module.path.replace("/", "\\")),
        is_executable = True,
    )
    ctx.actions.run(
        executable = "cmd.exe",
        arguments = ["/C", script.path.replace("/", "\\")],
        inputs = [out],
        tools = [script],
        outputs = [module],
        mnemonic = "CppWinRTModule",
        use_default_shell_env = True,
    )
    return [
        DefaultInfo(files = depset([out])),
        OutputGroupInfo(module = depset([module])),
        CcInfo(compilation_context = cc_common.create_compilation_context(
            headers = depset([out]),
            includes = depset([out.path]),
        )),
    ]

cppwinrt_component = rule(
    implementation = _cppwinrt_component_impl,
    attrs = {
        "winmds": attr.label_list(
            allow_files = [".winmd"],
            mandatory = True,
            doc = "The component's own metadata (`-in`): what `winrt_winmd` made of its IDL.",
        ),
        "references": attr.label_list(
            allow_files = [".winmd"],
            doc = "Metadata the component's types refer to (`-ref`): the SDK's, other components'.",
        ),
        "opts": attr.string_list(
            doc = "The project's other options from `<project>.vcxproj.cppwinrt_comp.rsp`, e.g. `-name X -pch pch.h -prefix -opt`.",
        ),
        "cppwinrt": attr.label(
            executable = True,
            cfg = "exec",
            allow_single_file = True,
            mandatory = True,
            doc = "cppwinrt.exe, from the Microsoft.Windows.CppWinRT NuGet package.",
        ),
    },
    doc = """Runs `cppwinrt -component` for a project that IMPLEMENTS WinRT classes.

Depend on the result for the headers (`<Class>.g.h`, `<Class>.g.cpp`,
`winrt/<Namespace>.h`). `module.g.cpp`, the activation factory table, is in the
output group `module`: put a `filegroup` with `output_group = "module"` in
`srcs` of the DLL.""",
)
