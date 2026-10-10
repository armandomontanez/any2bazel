"""The installed Windows SDK, pinned to one version, as a Bazel repository.

MSBuild resolves the Windows SDK from the machine; so does this rule -- but it
names the version, fails if that version is not installed, and exposes what a
build needs BY LABEL, so that actions declare SDK files as inputs instead of
embedding `C:\\Program Files (x86)\\Windows Kits\\...` paths.

Headers and import libraries are NOT exposed here: the C++ toolchain already
provides them (pin its SDK with BAZEL_WINSDK_FULL_VERSION to the same version).
This repository is for the things a toolchain does not carry:

  @<name>//:contract_winmds   the API contract metadata, in the SDK's layout
  @<name>//:winmds            the same files in one directory (short paths)
  @<name>//:platform_winmds   the contracts of the Windows platform itself
  @<name>//:union_winmd       the merged Windows.winmd
  @<name>//:bin/<arch>/<tool>.exe   midl, rc, mt, mdmerge, makepri, ...
"""

_DEFAULT_ROOTS = [
    "C:/Program Files (x86)/Windows Kits/10",
    "C:/Program Files/Windows Kits/10",
]

def _sdk_root(rctx):
    if rctx.attr.root:
        return rctx.attr.root
    candidates = []
    env = rctx.getenv("WindowsSdkDir")
    if env:
        candidates.append(env)
    reg = rctx.execute([
        "reg",
        "query",
        "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Microsoft SDKs\\Windows\\v10.0",
        "/v",
        "InstallationFolder",
    ])
    if reg.return_code == 0:
        for line in reg.stdout.splitlines():
            if "REG_SZ" in line:
                candidates.append(line.split("REG_SZ", 1)[1].strip())
    candidates.extend(_DEFAULT_ROOTS)
    for c in candidates:
        root = c.replace("\\", "/").rstrip("/")
        if rctx.path(root + "/Include").exists:
            return root
    fail("Windows SDK not found (looked at: %s)" % ", ".join(candidates))

def _windows_sdk_impl(rctx):
    root = _sdk_root(rctx)
    version = rctx.attr.version
    if not rctx.path("%s/Include/%s/um/Windows.h" % (root, version)).exists:
        installed = [p.basename for p in rctx.path(root + "/Include").readdir()]
        fail("Windows SDK %s is not installed under %s (installed: %s)" %
             (version, root, ", ".join(installed)))
    rctx.symlink("%s/References/%s" % (root, version), "References")
    rctx.symlink("%s/UnionMetadata/%s" % (root, version), "UnionMetadata")
    rctx.symlink("%s/bin/%s" % (root, version), "bin")

    # The same contract metadata in ONE directory. Under References each file
    # is <Contract>/<version>/<Contract>.winmd; below Bazel's execution root
    # the longest of those paths pass MAX_PATH, and midl cannot open them.
    for contract in rctx.path("%s/References/%s" % (root, version)).readdir():
        for contract_version in contract.readdir():
            for f in contract_version.readdir():
                if f.basename.endswith(".winmd"):
                    rctx.symlink(f, "winmd/" + f.basename)

    # The contracts that make up the Windows platform of this SDK version.
    # This is what a tool given the SDK BY VERSION (`cswinrt -input 10.0.x`,
    # `cppwinrt -in sdk`) reads: the list in Platform.xml, not every contract
    # installed under References.
    platform = rctx.read("%s/Platforms/UAP/%s/Platform.xml" % (root, version))
    for entry in platform.split("<ApiContract ")[1:]:
        name = entry.split('name="', 1)[1].split('"', 1)[0]
        contract_version = entry.split('version="', 1)[1].split('"', 1)[0]
        rctx.symlink(
            "%s/References/%s/%s/%s/%s.winmd" % (root, version, name, contract_version, name),
            "platform/%s.winmd" % name,
        )
    rctx.file("BUILD.bazel", """\
package(default_visibility = ["//visibility:public"])

filegroup(
    name = "contract_winmds",
    srcs = glob(["References/**/*.winmd"]),
)

# The contract metadata again, flat: use this one for midl and mdmerge.
filegroup(
    name = "winmds",
    srcs = glob(["winmd/*.winmd"]),
)

# The contracts of the Windows platform (Platform.xml): what `-input <SDK
# version>` means to cswinrt and cppwinrt.
filegroup(
    name = "platform_winmds",
    srcs = glob(["platform/*.winmd"]),
)

filegroup(
    name = "union_winmd",
    srcs = ["UnionMetadata/Windows.winmd"],
)

exports_files(glob(["bin/*/*.exe"]))
""")

windows_sdk = repository_rule(
    implementation = _windows_sdk_impl,
    attrs = {
        "version": attr.string(
            mandatory = True,
            doc = "Full SDK version, e.g. 10.0.26100.0: the reference build's WindowsTargetPlatformVersion.",
        ),
        "root": attr.string(doc = "SDK root; found from the environment or registry if empty."),
    },
    environ = ["WindowsSdkDir"],
    local = True,
    doc = "Exposes one version of the installed Windows SDK's metadata and tools.",
)
