"""Files of the .NET SDK itself, by label.

MSBuild passes more than a project's own packages to the C# compiler. For every
project that targets .NET 5 or later it adds, from the SDK directory:

  * the platform analyzers (`Sdks/Microsoft.NET.Sdk/analyzers/Microsoft.CodeAnalysis.NetAnalyzers.dll`
    and its C# companion), and
  * a global analyzer config that sets their severities for the project's
    analysis level (`.../analyzers/build/config/analysislevel_8_default.globalconfig`).

rules_dotnet downloads the same SDK for its toolchain but exposes only the
compiler and the runtime. This rule picks named files out of the toolchain's
SDK, so a target can pass them like MSBuild does:

    dotnet_sdk_files(
        name = "net_analyzers_files",
        paths = ["Sdks/Microsoft.NET.Sdk/analyzers/Microsoft.CodeAnalysis.NetAnalyzers.dll"],
    )
    import_library(name = "net_analyzers", analyzers = [":net_analyzers_files"], ...)

Paths are relative to the SDK's versioned directory (`sdk/<version>/`), which is
how the reference report spells them, so the SDK version is not repeated here.
"""

def _dotnet_sdk_files_impl(ctx):
    toolchain = ctx.toolchains["@rules_dotnet//dotnet:toolchain_type"]
    runtime = toolchain.runtime
    available = runtime[DefaultInfo].default_runfiles.files.to_list() + runtime[DefaultInfo].files.to_list()
    selected = []
    for wanted in ctx.attr.paths:
        found = None
        for f in available:
            # `.../sdk/<version>/<wanted>`, not a file of that name further down.
            below_sdk = f.path.split("/sdk/", 1)
            if len(below_sdk) == 2 and below_sdk[1].split("/", 1)[-1] == wanted:
                found = f
                break
        if not found:
            fail("the toolchain's .NET SDK has no file %s" % wanted)
        selected.append(found)
    return [DefaultInfo(files = depset(selected))]

dotnet_sdk_files = rule(
    implementation = _dotnet_sdk_files_impl,
    attrs = {
        "paths": attr.string_list(mandatory = True, doc = "Paths below `sdk/<version>/`."),
    },
    toolchains = ["@rules_dotnet//dotnet:toolchain_type"],
    doc = "Selects files of the toolchain's .NET SDK.",
)
