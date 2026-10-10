"""The analyzer config MSBuild generates for a project, with package files by label.

For every SDK-style project MSBuild writes
`obj/.../<Project>.GeneratedMSBuildEditorConfig.editorconfig` and passes it to
the compiler: the project properties analyzers and source generators may read
(`build_property.TargetFramework = ...`). The any2bazel emitter restates that
file with `write_file`, because its content is the reference build's.

A few of those properties are PATHS, and some source generators open what they
name. CsWin32 is given its Win32 metadata that way:

    build_property.CsWin32InputMetadataPaths = C:\\Users\\me\\.nuget\\packages\\microsoft.windows.sdk.win32metadata\\<version>\\Windows.Win32.winmd

Copied as is, the Bazel build would read the NuGet cache of whoever ran the
reference build. `msbuild_editorconfig` writes the same file with such a path
replaced by wherever Bazel has the file:

    msbuild_editorconfig(
        name = "Foo_msbuild_editorconfig",
        out = "_msbuild/Foo/msbuild.editorconfig",
        lines = ["is_global = true", "build_property.CsWin32InputMetadataPaths = {{0}}", ...],
        files = {"@nuget//microsoft.windows.sdk.win32metadata/<version>:Windows.Win32.winmd": "{{0}}"},
    )

The files must also reach the compile action (`compile_data` of a rules_dotnet
target, `compile_data` of `csc_assembly`): the config only names them.
"""

def _msbuild_editorconfig_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.out)
    content = "\n".join(ctx.attr.lines)
    for target, placeholder in ctx.attr.files.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("%s must be exactly one file" % target.label)

        # Relative to the directory the compiler runs in, like every other path
        # on its command line.
        content = content.replace(placeholder, files[0].path)
    ctx.actions.write(out, content)
    return [DefaultInfo(files = depset([out]))]

msbuild_editorconfig = rule(
    implementation = _msbuild_editorconfig_impl,
    attrs = {
        "out": attr.string(mandatory = True),
        "lines": attr.string_list(mandatory = True, doc = "The file, line by line; a final empty string ends the last line."),
        "files": attr.label_keyed_string_dict(allow_files = True, doc = "File -> the placeholder in `lines` its path replaces."),
    },
    doc = "Writes MSBuild's generated analyzer config, with paths of files Bazel provides.",
)
