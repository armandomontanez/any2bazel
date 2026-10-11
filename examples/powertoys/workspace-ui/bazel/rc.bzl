"""Windows resource scripts (`.rc` -> `.res`) as a Bazel action.

MSBuild compiles a project's `ResourceCompile` items with `rc.exe` and hands the
`.res` files to the linker as inputs, where they become the binary's version
information, icons and manifests. rules_cc has no resource compiler, so a
`cc_binary` on its own silently produces a binary without them.

    windows_resources(
        name = "version_info",
        src = "MyApp.rc",
        hdrs = ["resource.h"],
        deps = ["//src/common/version:headers"],   # anything the script #includes
        rc = "@windows_sdk//:bin/x64/rc.exe",
    )
    cc_binary(name = "MyApp", ..., deps = [":version_info"])

`rc.exe` resolves `#include <windows.h>` through INCLUDE, so the action runs in
the C++ toolchain's environment. Include directories and headers of `deps` are
passed on, the same way a compile would get them.
"""

load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

def _windows_resources_impl(ctx):
    cc_toolchain = find_cc_toolchain(ctx)
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    env = cc_common.get_environment_variables(
        feature_configuration = features,
        action_name = ACTION_NAMES.c_compile,
        variables = cc_common.create_compile_variables(
            feature_configuration = features,
            cc_toolchain = cc_toolchain,
        ),
    )
    context = cc_common.merge_cc_infos(
        cc_infos = [dep[CcInfo] for dep in ctx.attr.deps],
    ).compilation_context

    out = ctx.actions.declare_file(ctx.label.name + ".res")
    args = ctx.actions.args()
    args.add("/nologo")
    for define in ctx.attr.defines:
        args.add("/D", define)
    args.add_all(ctx.attr.opts)
    for directory in ctx.attr.includes:
        args.add("/I", directory)
    for directory in depset(transitive = [context.includes, context.quote_includes, context.system_includes]).to_list():
        args.add("/I", directory)
    args.add("/fo", out)
    args.add(ctx.file.src)
    ctx.actions.run(
        executable = ctx.executable.rc,
        arguments = [args],
        inputs = depset(
            [ctx.file.src] + ctx.files.hdrs,
            transitive = [context.headers, cc_toolchain.all_files],
        ),
        outputs = [out],
        env = env,
        mnemonic = "ResourceCompile",
        progress_message = "Compiling resources %{input}",
    )

    # A .res is a link input: depending on this target links it.
    linker_input = cc_common.create_linker_input(
        owner = ctx.label,
        user_link_flags = depset([out.path]),
        additional_inputs = depset([out]),
    )
    return [
        DefaultInfo(files = depset([out])),
        CcInfo(linking_context = cc_common.create_linking_context(
            linker_inputs = depset([linker_input]),
        )),
    ]

windows_resources = rule(
    implementation = _windows_resources_impl,
    attrs = {
        "src": attr.label(allow_single_file = [".rc"], mandatory = True),
        "hdrs": attr.label_list(allow_files = True, doc = "Files the script includes: headers, icons, manifests."),
        "deps": attr.label_list(providers = [CcInfo], doc = "Libraries whose headers the script includes."),
        "defines": attr.string_list(doc = "`/D` values, as the reference build passed them."),
        "includes": attr.string_list(doc = "`/I` directories of the workspace, relative to its root."),
        "opts": attr.string_list(doc = "Other rc options, for example `/l0x0409`."),
        "rc": attr.label(
            executable = True,
            cfg = "exec",
            allow_single_file = True,
            mandatory = True,
            doc = "rc.exe of the pinned SDK, for example `@windows_sdk//:bin/x64/rc.exe`.",
        ),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Compiles one resource script; depend on the result to link it.",
)
