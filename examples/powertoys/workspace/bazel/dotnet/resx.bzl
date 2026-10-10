"""`.resx` -> `.resources`, the step MSBuild's GenerateResource task performs.

The C# compiler embeds binary `.resources` files, not `.resx` XML. MSBuild
converts each `.resx` of a project before compiling and passes the result as
`/resource:obj/.../<Namespace>.<Path>.<Name>.resources`. rules_dotnet embeds
whatever files `resources` names, as they are, so the conversion needs its own
action:

    resx_resources(
        name = "Properties.Resources",
        src = "Properties/Resources.resx",
        out = "Properties.Resources.resources",
    )
    csharp_library(name = "Foo", resources = [":Properties.Resources"], ...)

The NAME a resource is embedded under is part of the assembly's contract
(`ResourceManager` looks it up). rules_dotnet derives it as
`<assembly name>.<path of the file in the package, with dots>`, so `out` is the
reference build's logical name without the assembly-name prefix, placed
directly in the package. (`csc_assembly` is told the name instead, and `out`
can be anything.)

Strings, typed values, embedded binary data and linked files are all converted
the way MSBuild converts them (see the tool's source). A linked file -- an
entry of type `ResXFileRef`, e.g. a form's icon -- is read during the
conversion and must be listed in `linked`.
"""

def _resx_resources_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.out)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [ctx.file.src.path, out.path],
        inputs = [ctx.file.src] + ctx.files.linked,
        outputs = [out],
        mnemonic = "ResxResources",
        progress_message = "Generating resources %{output}",
    )
    return [DefaultInfo(files = depset([out]))]

resx_resources = rule(
    implementation = _resx_resources_impl,
    attrs = {
        "src": attr.label(allow_single_file = [".resx"], mandatory = True),
        "out": attr.string(mandatory = True, doc = "File name; see the module comment for how it becomes the resource name."),
        "linked": attr.label_list(allow_files = True, doc = "Files the .resx refers to by path (ResXFileRef entries)."),
        "_tool": attr.label(
            default = "//bazel/dotnet/resx:resx2resources",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Converts one .resx into the .resources file the compiler embeds.",
)
