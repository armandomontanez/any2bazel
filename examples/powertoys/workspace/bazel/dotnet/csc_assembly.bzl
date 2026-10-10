"""A C# compile with exactly the inputs the reference build gave the compiler.

rules_dotnet's `csharp_library` decides part of the command line itself, and
three things MSBuild projects do cannot be stated through it (0.22.2):

  * An embedded resource is always named `<assembly name>.<path>`. MSBuild
    names it `<RootNamespace>.<path>`, and a project whose assembly name is
    not its root namespace (`PowerToys.ManagedCommon` / `ManagedCommon`) then
    finds none of its resources at run time.
  * A .NET Framework target is given every assembly of the reference pack
    (339 references where MSBuild passes 221). Two of the extra ones declare a
    `Microsoft.Windows` namespace, and `Windows.Foundation` written inside
    `namespace Microsoft...` stops meaning what it meant.
  * An ASP.NET target (`project_sdk = "web"`) is given every analyzer of the
    pack, including source generators MSBuild only runs on request.

`csc_assembly` runs the same compiler from the same rules_dotnet toolchain, and
passes what its attributes say and nothing else: the references, analyzers,
resources (with their names) and options are the reference build's. The
action is a `CSharpCompile` with a parameter file, like rules_dotnet's own, so
any2bazel reads it the same way.

`exact_csharp_assembly` wraps the result in rules_dotnet's public
`import_library`, so that `csharp_library`, `csharp_binary` and `csharp_test`
targets depend on it like on any other library. The reverse direction needs no
wrapper: a rules_dotnet library is a file to reference.

Use it where a `csharp_library` cannot reproduce the reference build, not as a
replacement: it has none of rules_dotnet's handling of target frameworks,
run-time dependencies or executables.
"""

load("@rules_dotnet//dotnet:defs.bzl", "import_library")

CscAssemblyInfo = provider(
    doc = "An assembly compiled by csc_assembly.",
    fields = {
        "assembly": "File: the assembly.",
        "ref": "File: its reference assembly (what dependents compile against).",
    },
)

_REFERENCE_EXTENSIONS = ("dll", "exe", "winmd")

def _references(targets):
    """Files to pass as /reference for a list of dependencies: the reference
    assembly of another csc_assembly, otherwise every assembly among a
    target's files."""
    files = []
    for target in targets:
        if CscAssemblyInfo in target:
            files.append(target[CscAssemblyInfo].ref)
        else:
            files.extend([f for f in target[DefaultInfo].files.to_list() if f.extension.lower() in _REFERENCE_EXTENSIONS])
    return files

def _csc_assembly_impl(ctx):
    toolchain = ctx.toolchains["@rules_dotnet//dotnet:toolchain_type"]
    runtime = toolchain.runtime
    compiler = toolchain.csharp_compiler
    assembly_name = ctx.attr.assembly_name or ctx.label.name
    directory = "%s/%s" % (ctx.label.name, ctx.attr.target_framework or "bin")

    # An executable is a .dll too (the host loads it); only .NET Framework
    # writes an .exe.
    extension = "exe" if ctx.attr.kind != "library" and ctx.attr.target_framework.startswith("net4") else "dll"
    out = ctx.actions.declare_file("%s/%s.%s" % (directory, assembly_name, extension))
    ref = ctx.actions.declare_file("%s/ref/%s.%s" % (directory, assembly_name, extension))
    pdb = ctx.actions.declare_file("%s/%s.pdb" % (directory, assembly_name))
    outputs = [out, ref, pdb]

    references = _references(ctx.attr.refs)
    analyzers = [f for f in ctx.files.analyzers if f.extension.lower() == "dll"]

    # `/noconfig` is ignored in a parameter file: the wrapper passes it.
    args = ctx.actions.args()
    args.add("/nologo")
    args.add("/utf8output")
    args.add("/target:" + ctx.attr.kind)
    args.add("/out:" + out.path)
    args.add("/refout:" + ref.path)
    args.add("/debug:portable")
    args.add("/pdb:" + pdb.path)
    optimized = ctx.var["COMPILATION_MODE"] == "opt"
    args.add("/optimize+" if optimized else "/optimize-")
    args.add("/define:" + ("RELEASE" if optimized else "DEBUG"))
    args.add_all(ctx.attr.defines, format_each = "/define:%s")
    args.add_all(ctx.attr.options)
    if ctx.attr.generate_documentation_file:
        doc = ctx.actions.declare_file("%s/%s.xml" % (directory, assembly_name))
        outputs.append(doc)
        args.add("/doc:" + doc.path)
    if ctx.file.keyfile:
        args.add("/keyfile:" + ctx.file.keyfile.path)
    if ctx.file.win32_icon:
        args.add("/win32icon:" + ctx.file.win32_icon.path)
    if ctx.file.win32_manifest:
        args.add("/win32manifest:" + ctx.file.win32_manifest.path)
    args.add_all(references, format_each = "/reference:%s")
    args.add_all(analyzers, format_each = "/analyzer:%s")
    args.add_all(ctx.files.additionalfiles, format_each = "/additionalfile:%s")
    args.add_all(ctx.files.analyzer_configs, format_each = "/analyzerconfig:%s")
    resources = []
    for target, logical_name in ctx.attr.resources.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("resource %s must be exactly one file" % target.label)
        resources.append(files[0])
        args.add("/resource:%s,%s" % (files[0].path, logical_name))
    args.add_all(ctx.files.srcs, expand_directories = True)
    args.set_param_file_format("multiline")
    args.use_param_file("@%s", use_always = True)

    loose = [f for f in [ctx.file.keyfile, ctx.file.win32_icon, ctx.file.win32_manifest] if f]
    ctx.actions.run(
        mnemonic = "CSharpCompile",
        progress_message = "Compiling %s (exact)" % ctx.label,
        executable = ctx.executable._wrapper,
        arguments = [runtime.files_to_run.executable.path, compiler.files_to_run.executable.path, args],
        inputs = depset(
            ctx.files.srcs + references + analyzers + ctx.files.additionalfiles +
            ctx.files.analyzer_configs + ctx.files.compile_data + resources + loose +
            [runtime.files_to_run.executable, compiler.files_to_run.executable],
            transitive = [runtime.default_runfiles.files, compiler.default_runfiles.files],
        ),
        outputs = outputs,
        env = {
            "DOTNET_CLI_HOME": runtime.files_to_run.executable.dirname,
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
        },
    )
    return [
        DefaultInfo(files = depset([out])),
        CscAssemblyInfo(assembly = out, ref = ref),
        OutputGroupInfo(ref = depset([ref]), pdb = depset([pdb])),
    ]

csc_assembly = rule(
    implementation = _csc_assembly_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, doc = "Sources; a directory output counts as its files."),
        "assembly_name": attr.string(doc = "Defaults to the target name."),
        "kind": attr.string(default = "library", values = ["library", "exe", "winexe"]),
        "target_framework": attr.string(doc = "Names the output directory, e.g. `net48`."),
        "refs": attr.label_list(allow_files = True, doc = "Everything the compiler may reference: assemblies, metadata, other csc_assembly targets, rules_dotnet libraries."),
        "analyzers": attr.label_list(allow_files = True, doc = "Analyzer and source generator assemblies."),
        "defines": attr.string_list(),
        "options": attr.string_list(doc = "Further compiler options, as csc spells them."),
        "resources": attr.label_keyed_string_dict(allow_files = True, doc = "File to embed -> its logical name."),
        "additionalfiles": attr.label_list(allow_files = True),
        "analyzer_configs": attr.label_list(allow_files = True),
        "compile_data": attr.label_list(allow_files = True, doc = "Files the compile reads that no option names (what an analyzer config points a source generator at)."),
        "keyfile": attr.label(allow_single_file = True),
        "win32_icon": attr.label(allow_single_file = True),
        "win32_manifest": attr.label(allow_single_file = True),
        "generate_documentation_file": attr.bool(default = False),
        "_wrapper": attr.label(default = Label("//bazel/dotnet:csc_wrapper.bat"), allow_single_file = True, executable = True, cfg = "exec"),
    },
    toolchains = ["@rules_dotnet//dotnet:toolchain_type"],
    doc = "Compiles one C# assembly with exactly the listed inputs.",
)

def exact_csharp_assembly(name, assembly_name = None, version = "1.0.0", deps = [], visibility = None, tags = [], **kwargs):
    """A `csc_assembly` that rules_dotnet targets can depend on.

    An executable (`kind = "exe"`) is wrapped the same way: its assembly can be
    referenced, by its tests for one. It is not runnable: there is no host
    beside it, which is `csharp_binary`'s business.

    Args:
      name: the assembly; `<name>_csc` is the compile.
      assembly_name: defaults to `name`.
      version: assembly version for rules_dotnet's bookkeeping.
      deps: rules_dotnet targets this assembly needs at run time (they reach
        whoever depends on it); what it compiles against is `refs`.
      visibility: of both targets.
      tags: of both targets.
      **kwargs: attributes of `csc_assembly`.
    """
    csc_assembly(
        name = name + "_csc",
        assembly_name = assembly_name or name,
        visibility = visibility,
        tags = tags,
        **kwargs
    )
    native.filegroup(
        name = name + "_ref",
        srcs = [":" + name + "_csc"],
        output_group = "ref",
        visibility = visibility,
        tags = tags,
    )
    import_library(
        name = name,
        library_name = assembly_name or name,
        version = version,
        refs = [":" + name + "_ref"],
        libs = [":" + name + "_csc"],
        deps = deps,
        visibility = visibility,
        tags = tags,
    )
