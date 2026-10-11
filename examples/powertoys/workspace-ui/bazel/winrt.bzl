"""WinRT metadata (`.winmd`) from IDL, as Bazel actions.

A WinRT component project describes its API in MIDL 3 (`.idl`). MSBuild's
C++/WinRT targets turn that into metadata in two steps, and every language
projection (C++/WinRT for the component itself, CsWinRT for C# callers) is
generated from the result:

    midl /winrt /winmd <IntDir>Unmerged\\X.winmd  /reference <each SDK contract>  X.idl
    mdmerge -i <IntDir>Unmerged\\X.winmd -o <IntDir>Merged -partial -n:<depth>
            -metadata_dir <each SDK contract directory>

The merged `.winmd` is then copied into the shared output tree, where other
projects pick it up by path. Here it is the output of a target, and those
projects name the label.

Producing the metadata does not compile any of the component's C++: a C#
project that only needs the projection does not wait for the native build.

`midl` preprocesses with `cl.exe` and resolves `import "x.idl"` through INCLUDE,
so the actions run in the C++ toolchain's environment (the pinned compiler and
SDK). The SDK's own metadata and both tools come from `windows_sdk.bzl`.
"""

load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

def _dirname(path):
    return path.rsplit("/", 1)[0]

def _foundation_dir(winmds):
    for f in winmds:
        if f.basename == "Windows.Foundation.FoundationContract.winmd":
            return f.dirname
    fail("sdk_winmds has no Windows.Foundation.FoundationContract.winmd")

def _winrt_winmd_impl(ctx):
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
    sdk = ctx.files.sdk_winmds
    references = sdk + ctx.files.references

    # 1. midl: one unmerged .winmd per .idl.
    unmerged = []
    for idl in ctx.files.srcs:
        out = ctx.actions.declare_file("%s.unmerged/%s.winmd" % (ctx.label.name, idl.basename[:-len(".idl")]))
        unmerged.append(out)
        args = ctx.actions.args()
        args.add("/metadata_dir", _foundation_dir(sdk))
        args.add("/winrt")
        args.add("/nologo")
        args.add_all(ctx.attr.opts)
        args.add("/winmd", out)

        # A WinRT interface needs no header, proxy or stub from this step.
        args.add_all(["/h", "nul", "/dlldata", "nul", "/iid", "nul", "/proxy", "nul", "/notlb"])
        args.add_all(["/client", "none", "/server", "none", "/nomidl"])
        for directory in depset([f.dirname for f in ctx.files.imports]).to_list():
            args.add("/I", directory)
        for winmd in references:
            args.add("/reference", winmd)

        # With backslashes, as MSBuild spells it. midl derives the IID of a
        # synthesized interface from more than its declaration: given
        # `a/b/X.idl` it produced a different IID for one default interface
        # of winget's configuration API than given `a\\b\\X.idl`, and so a
        # component that no other build of the same IDL can talk to.
        args.add(idl.path.replace("/", "\\"))
        ctx.actions.run(
            executable = ctx.executable.midl,
            arguments = [args],
            inputs = depset([idl] + ctx.files.imports + references, transitive = [cc_toolchain.all_files]),
            outputs = [out],
            env = env,
            mnemonic = "MidlRT",
            progress_message = "Compiling WinRT interface definition %{input}",
        )

    # 2. mdmerge: the component's metadata, named after its root namespace.
    merged = ctx.actions.declare_file("%s/%s.winmd" % (ctx.label.name, ctx.attr.namespace))
    args = ctx.actions.args()
    args.add("-v")
    for directory in depset([f.dirname for f in references]).to_list():
        args.add("-metadata_dir", directory)
    for winmd in unmerged:
        args.add("-i", winmd)
    args.add("-o", merged.dirname)
    args.add("-partial")
    args.add("-n:%d" % len(ctx.attr.namespace.split(".")))
    ctx.actions.run(
        executable = ctx.executable.mdmerge,
        arguments = [args],
        inputs = unmerged + references,
        outputs = [merged],
        mnemonic = "MdMerge",
        progress_message = "Merging WinRT metadata %{output}",
    )
    return [DefaultInfo(files = depset([merged]))]

winrt_winmd = rule(
    implementation = _winrt_winmd_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = [".idl"], mandatory = True, doc = "The interface definitions to compile."),
        "imports": attr.label_list(allow_files = [".idl", ".h"], doc = "Project files the definitions import."),
        "namespace": attr.string(mandatory = True, doc = "Root namespace: the merged file is `<namespace>.winmd`."),
        "references": attr.label_list(allow_files = [".winmd"], doc = "Metadata of other components the definitions use."),
        "sdk_winmds": attr.label_list(
            allow_files = [".winmd"],
            mandatory = True,
            doc = "The Windows SDK's contract metadata, e.g. `@windows_sdk//:contract_winmds`.",
        ),
        "opts": attr.string_list(
            default = ["/W1", "/char", "signed", "/env", "x64", "/target", "NT60"],
            doc = "The project's other midl options, as the reference build passed them.",
        ),
        "midl": attr.label(executable = True, cfg = "exec", allow_single_file = True, mandatory = True),
        "mdmerge": attr.label(executable = True, cfg = "exec", allow_single_file = True, mandatory = True),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Compiles MIDL 3 into the component's merged .winmd.",
)
