# Patches for rules_dotnet

Two places where rules_dotnet (0.22.2) cannot reproduce what an MSBuild C#
project does, fixed at the source. Both were found by migrating winget-cli and
PowerToys ([case study, section 10](../../docs/CASE-winget-cli-migration.md));
the full list of gaps and the workarounds that need no patch is in
[REFERENCE-msbuild-csharp.md](../../docs/REFERENCE-msbuild-csharp.md).

| Patch | Without it | With it |
|---|---|---|
| `0001-honor-winexe.patch` | `winexe = True` is accepted and the compile still says `/target:exe`: a GUI program opens a console window | `winexe = True` compiles with `/target:winexe` (C# and F#, binaries and tests) |
| `0002-named-resources.patch` | an embedded resource is always named `<assembly name>.<path>`; a project whose root namespace is not its assembly name finds none of its resources at run time | a new attribute, `named_resources = {file: logical name}`, next to `resources` on the C# and F# library, binary and test rules |

They are independent: take either or both.

## Using them

Copy the `.patch` files into a package of your workspace (a directory with a
`BUILD.bazel`, which may be empty), and in the root `MODULE.bazel`:

```starlark
bazel_dep(name = "rules_dotnet", version = "0.22.2")

single_version_override(
    module_name = "rules_dotnet",
    patch_strip = 1,
    patches = [
        "//bazel/patches:0001-honor-winexe.patch",
        "//bazel/patches:0002-named-resources.patch",
    ],
)
```

`single_version_override` only has effect in the root module. The patches are
made against 0.22.2 and touch `dotnet/private/`; expect to refresh them for
another version.

```starlark
csharp_binary(
    name = "app",
    srcs = ["Program.cs"],
    winexe = True,              # MSBuild: <OutputType>WinExe</OutputType>
    ...
)

csharp_library(
    name = "common",
    out = "Contoso.Common",     # the assembly name
    # MSBuild: <RootNamespace>Common</RootNamespace>, so Properties/Strings.resx
    # is embedded as Common.Properties.Strings.resources.
    named_resources = {":Properties.Strings_resx": "Common.Properties.Strings.resources"},
    resources = ["logo.png"],   # still Contoso.Common.logo.png
    ...
)
```

The logical name of every resource of the reference build is in the report
`extract_msbuild.py` writes (`targets.<name>.csharp.resources[].name`).

## How they relate to the emitter and the templates

`emit_dotnet.py` does not assume the patches. Without them it adds
`/target:winexe` to `compiler_options` (harmless with the first patch applied),
and routes a project whose resource names rules_dotnet cannot give through the
`csc_assembly.bzl` template. With the second patch such a project can be an
ordinary `csharp_library` with `named_resources` instead; the emitter does not
write that yet.

## What was checked

In a workspace with both patches applied through `single_version_override`
(Bazel 9.2, Windows, .NET SDK 10.0.400):

- `winexe = True`: the compile action has `/target:winexe` (`--target:winexe`
  for F#) and the assembly's PE subsystem is `WINDOWS_GUI`; a binary without the
  attribute is unchanged (`/target:exe`, `WINDOWS_CUI`).
- `named_resources` with a source file and a generated file: the compile action
  has `/resource:<path>,<name>` for each (`--resource:` for F#), a file in
  `resources` keeps its `<assembly>.<path>` name, and a test that lists the
  built assembly's `GetManifestResourceNames()` sees exactly the three names.
  On a `csharp_binary` the attribute works the same and the program still runs.
- Each patch applied alone: builds, and only its own behaviour changes.

Not done, and needed before upstreaming: rules_dotnet's own tests and generated
API docs (`docs/*.md`) are not part of the patches, and the changes were not
run on Linux or macOS (they add nothing platform-specific).
