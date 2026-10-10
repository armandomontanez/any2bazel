"""MIDL (RPC interface definitions) as a Bazel action.

In MSBuild the `Midl` item of ONE project runs `midl.exe` with that project's
directory as the working directory, so the generated header and stubs
(`<name>.h`, `<name>_c.c`, `<name>_s.c`) land IN THE SOURCE TREE beside the
`.idl`, and other projects then compile those files by relative path. Nothing
declares that those projects need the first one's MIDL step: it works because
the solution happens to build them in order.

Here the outputs are declared files of this target; whoever compiles a stub or
includes the header names the label, and Bazel orders the work.

`midl.exe` preprocesses with `cl.exe` and resolves `import "x.idl"` through
INCLUDE, so the action runs in the C++ toolchain's environment -- the same
pinned compiler and SDK as every compile.

Exercised for classic RPC interfaces (`/client stub /server stub`). WinRT IDL
(`midl /winrt` + `mdmerge` + a C++/WinRT component projection) is a different
pipeline and is not covered by this rule.
"""

load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

def _midl_impl(ctx):
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

    outputs = [ctx.outputs.header]
    args = ctx.actions.args()
    args.add("/nologo")
    args.add_all(ctx.attr.opts)
    for idl in ctx.files.imports:
        args.add("/I", idl.dirname)
    args.add("/out", ctx.outputs.header.dirname)
    args.add("/h", ctx.outputs.header.basename)
    for kind, out in (("client", ctx.outputs.client_stub), ("server", ctx.outputs.server_stub)):
        if out:
            if out.dirname != ctx.outputs.header.dirname:
                fail("%s stub must be declared in the same package as the header" % kind)
            args.add("/" + kind, "stub")
            args.add("/%sstub" % kind[0], out.basename)
            outputs.append(out)
        else:
            args.add("/" + kind, "none")

    # With backslashes, as MSBuild spells it: see winrt.bzl.
    args.add(ctx.file.src.path.replace("/", "\\"))

    ctx.actions.run(
        executable = ctx.executable.midl,
        arguments = [args],
        inputs = depset([ctx.file.src] + ctx.files.imports, transitive = [cc_toolchain.all_files]),
        outputs = outputs,
        env = env,
        mnemonic = "Midl",
        progress_message = "Compiling interface definition %{input}",
    )
    return [DefaultInfo(files = depset(outputs))]

midl = rule(
    implementation = _midl_impl,
    attrs = {
        "src": attr.label(allow_single_file = [".idl"], mandatory = True),
        "imports": attr.label_list(
            allow_files = [".idl", ".acf", ".h"],
            doc = "Project files the IDL imports. SDK files are found through the toolchain.",
        ),
        "header": attr.output(mandatory = True, doc = "Generated header (`/h`)."),
        "client_stub": attr.output(doc = "Client stub (`/client stub /cstub`); omitted: `/client none`."),
        "server_stub": attr.output(doc = "Server stub (`/server stub /sstub`); omitted: `/server none`."),
        "opts": attr.string_list(
            doc = "The project's other midl options, as the reference build passed them " +
                  "(for example `/W1 /char signed /env x64 /target NT60`).",
        ),
        "midl": attr.label(
            executable = True,
            cfg = "exec",
            allow_single_file = True,
            mandatory = True,
            doc = "midl.exe of the pinned SDK, for example `@windows_sdk//:bin/x64/midl.exe`.",
        ),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Runs midl.exe over one RPC interface definition; outputs are addressable by label.",
)
