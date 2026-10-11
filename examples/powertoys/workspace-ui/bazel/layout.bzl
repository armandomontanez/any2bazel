"""An output directory laid out the way the MSBuild build laid it out.

MSBuild projects do not hand their outputs to each other: they write into a
shared directory, copy what they reference beside what they build, and at run
time programs find each other and their libraries by relative path. A Bazel
target's outputs are wherever Bazel puts them, each in its own place. This rule
puts a chosen set of files into ONE directory under the names the reference
build gave them:

    program_directory(
        name = "App",
        executable = "App.exe",
        files = {
            "App.exe": ":App_host",
            "App.dll": ":App_csc",
            "App.runtimeconfig.json": ":App_runtimeconfig",
            "Lib.dll": "//lib:Lib",
            "native/Thing.dll": "//native:Thing.dll",
            ...
        },
    )

`bazel run //pkg:App` then starts `bazel-bin/pkg/App/App.exe`, which finds its
neighbours like the reference build's program does. `output_directory` is the
same without a program to start: a product tree, a plugin directory.

Each entry is a destination path and a target. A target with several files
gives the one whose name is the destination's file name.

The files are symbolic links where Bazel may make them (Windows: Developer Mode
and `startup --windows_enable_symlinks`), copies otherwise.
"""

def _lay_out(ctx):
    outputs = []
    executable = None
    wanted = getattr(ctx.attr, "executable", "")
    for destination, target in ctx.attr.files.items():
        files = target[DefaultInfo].files.to_list()
        name = destination.rsplit("/", 1)[-1].lower()
        chosen = [f for f in files if f.basename.lower() == name]
        if not chosen and len(files) == 1:
            chosen = files
        if len(chosen) != 1:
            fail("%s: %s has %d files and %s named %s" % (
                destination,
                target.label,
                len(files),
                "none" if not chosen else "several",
                name,
            ))
        out = ctx.actions.declare_file("%s/%s" % (ctx.label.name, destination))
        ctx.actions.symlink(output = out, target_file = chosen[0], is_executable = destination == wanted)
        outputs.append(out)
        if destination == wanted:
            executable = out
    if wanted and not executable:
        fail("executable %s is not among the files" % wanted)
    return outputs, executable

def _output_directory_impl(ctx):
    outputs, _ = _lay_out(ctx)
    return [DefaultInfo(files = depset(outputs))]

def _program_directory_impl(ctx):
    outputs, executable = _lay_out(ctx)
    return [DefaultInfo(
        files = depset(outputs),
        executable = executable,
        runfiles = ctx.runfiles(files = outputs),
    )]

_FILES = attr.string_keyed_label_dict(
    allow_files = True,
    mandatory = True,
    doc = "Destination path below the directory -> the target providing the file.",
)

output_directory = rule(
    implementation = _output_directory_impl,
    attrs = {"files": _FILES},
    doc = "Files of many targets in one directory, under given names.",
)

program_directory = rule(
    implementation = _program_directory_impl,
    attrs = {
        "files": _FILES,
        "executable": attr.string(mandatory = True, doc = "Destination path of the program `bazel run` starts."),
    },
    executable = True,
    doc = "An output directory with a program in it, runnable with `bazel run`.",
)
