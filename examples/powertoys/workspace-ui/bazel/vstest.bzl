"""Running a native unit-test DLL under `bazel test`.

A Microsoft C++ unit test project (`CppUnitTest.h`) does not build a program.
It builds a DLL that exports test metadata, and `vstest.console.exe` loads it
through an adapter that ships with Visual Studio. MSBuild only builds the DLL;
running it is a separate CI step. So there is nothing for `cc_test` to execute.

    cc_binary(name = "Foo.Tests.dll", linkshared = True, ..., deps = ["@msvc//:cppunittest"])
    vstest_test(name = "foo_tests", dll = ":Foo.Tests.dll")

The runner comes from the pinned Visual Studio instance (`@msvc//:defs.bzl`),
not from `vswhere -latest`: the adapter must match the framework the DLL was
compiled against. A TRX report is written to the test's undeclared outputs.

Limits: the runner and its adapter are host files that Bazel does not track,
and vstest decides the exit code (non-zero if any test fails).
"""

load("@msvc//:defs.bzl", "VSTEST_CONSOLE")

_LAUNCHER = """@echo off
setlocal
set "DLL="
if defined RUNFILES_MANIFEST_FILE (
  for /f "usebackq tokens=1,*" %%a in ("%RUNFILES_MANIFEST_FILE%") do if "%%a"=="{key}" set "DLL=%%b"
)
if not defined DLL if exist "%~dp0{name}.runfiles\\{key_native}" set "DLL=%~dp0{name}.runfiles\\{key_native}"
if not defined DLL (
  echo vstest_test: cannot locate {key} in the runfiles
  exit /b 2
)
set "RESULTS=%TEST_UNDECLARED_OUTPUTS_DIR%"
if not defined RESULTS set "RESULTS=%TEMP%"
"{vstest}" "%DLL%" /Platform:{platform} "/ResultsDirectory:%RESULTS%" /Logger:trx {args} %*
exit /b %ERRORLEVEL%
"""

def _vstest_test_impl(ctx):
    if not VSTEST_CONSOLE:
        fail("vstest.console.exe was not found in the pinned Visual Studio instance")
    dll = ctx.file.dll
    key = ctx.workspace_name + "/" + dll.short_path
    launcher = ctx.actions.declare_file(ctx.label.name + ".bat")
    ctx.actions.write(
        launcher,
        _LAUNCHER.format(
            key = key,
            key_native = key.replace("/", "\\"),
            name = ctx.label.name + ".bat",
            vstest = VSTEST_CONSOLE.replace("/", "\\"),
            platform = ctx.attr.platform,
            args = " ".join(ctx.attr.vstest_args),
        ).replace("\n", "\r\n"),
        is_executable = True,
    )
    return [DefaultInfo(
        executable = launcher,
        runfiles = ctx.runfiles(files = [dll] + ctx.files.data),
    )]

vstest_test = rule(
    implementation = _vstest_test_impl,
    test = True,
    attrs = {
        "dll": attr.label(allow_single_file = [".dll"], mandatory = True, doc = "The test DLL."),
        "data": attr.label_list(allow_files = True, doc = "Files the tests read at run time."),
        "platform": attr.string(default = "x64"),
        "vstest_args": attr.string_list(
            doc = "Extra vstest.console.exe arguments. They are pasted into a batch file: " +
                  "wrap one that contains `&` or `|` in double quotes, for example " +
                  "'\"/TestCaseFilter:Name!=A&Name!=B\"'.",
        ),
    },
    doc = "Runs a CppUnitTest DLL with vstest.console.exe from the pinned Visual Studio instance.",
)
