"""MSVC precompiled headers (`/Yc`, `/Yu`) for rules_cc targets.

rules_cc has no precompiled headers for MSVC, and compiling without one is not
the same build, only slower. With `/Yu"pch.h"` the compiler does not open
`pch.h` at all: it takes everything up to `#include "pch.h"` from the `.pch`.
Without it, the directive is an ordinary include, looked up beside the source
first. A source one project shares with another (`..\\Other\\trace.cpp`) then
finds the OTHER project's `pch.h`, and a generated source that sits in an output
directory finds none.

    msvc_precompiled_header(
        name = "app_pch",
        src = "pch.cpp",              # the project's /Yc source
        header = "pch.h",             # the name on /Yc and /Yu
        hdrs = [...],                 # what pch.h includes, like a cc_library's headers
        copts = COPTS,                # exactly the consumer's
        local_defines = DEFINES,
        deps = [...],
        alwayslink = True,            # a program's objects are all linked
    )

    cc_binary(
        name = "app",
        srcs = [...],                 # without pch.cpp
        copts = COPTS + ["/Yupch.h", "/Fp$(execpath :app_pch)"],
        additional_compiler_inputs = [":app_pch"],
        local_defines = DEFINES,
        deps = [..., ":app_pch"],     # brings the object the /Yc compile wrote
    )

The compiler rejects a `.pch` built with other options than the compile using
it (C4651 and friends, errors under `/WX`), so both sides must be given the same
`copts`, `local_defines`, `features` and `deps`. The rule builds its command
line through the C++ toolchain, so what Bazel itself adds (`--copt`, `-c opt`,
feature flags) is the same on both.
"""

load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

def _msvc_precompiled_header_impl(ctx):
    cc_toolchain = find_cc_toolchain(ctx)
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    merged = cc_common.merge_cc_infos(cc_infos = [dep[CcInfo] for dep in ctx.attr.deps])
    context = merged.compilation_context

    pch = ctx.actions.declare_file(ctx.label.name + ".pch")
    obj = ctx.actions.declare_file(ctx.label.name + ".obj")
    variables = cc_common.create_compile_variables(
        feature_configuration = features,
        cc_toolchain = cc_toolchain,
        user_compile_flags = ctx.fragments.cpp.copts + ctx.fragments.cpp.cxxopts + ctx.attr.copts,
        source_file = ctx.file.src.path,
        output_file = obj.path,
        include_directories = context.includes,
        # What every rules_cc compile searches besides its dependencies' directories.
        quote_include_directories = depset([".", ctx.bin_dir.path], transitive = [context.quote_includes]),
        system_include_directories = context.system_includes,
        preprocessor_defines = depset(ctx.attr.local_defines, transitive = [context.defines]),
    )
    arguments = cc_common.get_memory_inefficient_command_line(
        feature_configuration = features,
        action_name = ACTION_NAMES.cpp_compile,
        variables = variables,
    )
    env = cc_common.get_environment_variables(
        feature_configuration = features,
        action_name = ACTION_NAMES.cpp_compile,
        variables = variables,
    )
    ctx.actions.run(
        executable = cc_common.get_tool_for_action(
            feature_configuration = features,
            action_name = ACTION_NAMES.cpp_compile,
        ),
        arguments = arguments + ["/Yc" + ctx.attr.header, "/Fp" + pch.path],
        inputs = depset(
            [ctx.file.src] + ctx.files.hdrs,
            transitive = [context.headers, cc_toolchain.all_files],
        ),
        outputs = [pch, obj],
        env = env,
        mnemonic = "CppCompile",
        progress_message = "Precompiling %{input}",
    )

    # The object holds what the .pch refers to, and goes where objects compiled
    # with this .pch go. Under MSBuild it is one more object of the project: a
    # member of a static library, or on the link line of a program. Here it is
    # a library of its own, linked whole (`alwayslink`) for a program.
    linking_context, _ = cc_common.create_linking_context_from_compilation_outputs(
        actions = ctx.actions,
        name = ctx.label.name,
        feature_configuration = features,
        cc_toolchain = cc_toolchain,
        compilation_outputs = cc_common.create_compilation_outputs(
            objects = depset([obj]),
            pic_objects = depset([obj]),
        ),
        linking_contexts = [merged.linking_context],
        alwayslink = ctx.attr.alwayslink,
        disallow_dynamic_library = True,
    )
    return [
        DefaultInfo(files = depset([pch])),
        CcInfo(compilation_context = context, linking_context = linking_context),
    ]

msvc_precompiled_header = rule(
    implementation = _msvc_precompiled_header_impl,
    attrs = {
        "src": attr.label(allow_single_file = [".cpp", ".cc", ".cxx", ".c"], mandatory = True, doc = "The source compiled with `/Yc`."),
        "header": attr.string(mandatory = True, doc = "The header name as `/Yc` and `/Yu` spell it, e.g. `pch.h`."),
        "hdrs": attr.label_list(allow_files = True, doc = "Headers of the workspace the precompiled header includes."),
        "copts": attr.string_list(doc = "The compiler options of the target that uses the header, unchanged."),
        "local_defines": attr.string_list(doc = "Its `local_defines`, unchanged."),
        "deps": attr.label_list(providers = [CcInfo], doc = "Its compile dependencies."),
        "alwayslink": attr.bool(default = False, doc = "Link the object even if nothing refers to it: for a program or DLL, whose project hands every object to the linker. Leave it off for a static library, whose members are taken on demand."),
    },
    toolchains = use_cc_toolchain(),
    fragments = ["cpp"],
    doc = "Compiles a precompiled header; the `.pch` is the target's file, the object comes with `deps`.",
)
