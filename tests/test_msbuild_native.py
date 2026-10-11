# Copyright 2026 EngFlow Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""What the MSBuild frontend records for the C++ emitter: packages a native
target takes from NuGet, resource scripts, optimization options, hashes."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import extract_msbuild
import nuget_assets
from model import Action
from msbuild_origins import Origins

REPO = r"C:\src\repo"
NUGET = r"C:\Users\me\.nuget\packages"


def _report():
    return {
        "targets": {
            "runner": {
                "project": "src/runner/runner.vcxproj", "kind": "executable",
                "compile": {"variants": [{"sources": [
                    "src/runner/main.cpp",
                    "C:/Users/me/.nuget/packages/contoso.sdk/2.0.0/include/AutoInitializer.cpp"]}]},
                "steps": [{"kind": "write", "file": "src/runner/x64/Release/runner.vcxproj.cppwinrt_ref.rsp",
                           "lines": ['-in "%s\\contoso.sdk\\2.0.0\\metadata\\Contoso.winmd" '
                                     '-out "x64\\Release\\Generated Files\\."' % NUGET]}]},
            "lib": {"project": "src/lib/lib.vcxproj", "kind": "static_library"},
            "Managed": {"project": "src/m/m.csproj", "csharp": {}},
        },
        "externals": [
            {"kind": "nuget", "id": "contoso.sdk", "version": "2.0.0", "integrity": "sha512-AAAA",
             "source": "https://feed.example/v3/index.json", "root": NUGET + r"\contoso.sdk\2.0.0",
             "include_dirs": ["include"], "libs": ["lib/native/x64/Contoso.lib"],
             "consumers": ["runner", "lib", "Managed"]},
            {"kind": "nuget", "id": "Contoso.Tool", "version": "1.2.3", "integrity": "sha512-BBBB",
             "root": REPO + r"\packages\Contoso.Tool.1.2.3", "tools": ["bin/tool"], "consumers": ["lib"]},
            # Only contributed build logic: nothing to download.
            {"kind": "nuget", "id": "contoso.build", "version": "1.0.0", "integrity": "sha512-CCCC",
             "root": NUGET + r"\contoso.build\1.0.0", "build_imports": ["build/x.targets"],
             "consumers": ["runner"]},
            # Used by managed code only: the assets file covers it.
            {"kind": "nuget", "id": "contoso.managed", "version": "1.0.0", "integrity": "sha512-DDDD",
             "root": NUGET + r"\contoso.managed\1.0.0", "include_dirs": ["include"],
             "consumers": ["Managed"]},
        ],
    }


def test_what_a_native_target_takes_from_a_package_is_what_its_command_lines_name():
    packages = nuget_assets.native_packages(_report())
    assert sorted(packages) == ["contoso.sdk/2.0.0", "contoso.tool/1.2.3"]
    sdk = packages["contoso.sdk/2.0.0"]
    assert sdk["cc"] == {"include_dirs": ["include"], "libs": ["lib/native/x64/Contoso.lib"]}
    assert sdk["consumers"] == ["lib", "runner"]            # not the managed project
    # A source the package has its consumers compile, and metadata a response
    # file hands to a generator: addressable files of the package.
    assert sdk["files"] == {"runner": ["include/AutoInitializer.cpp", "metadata/Contoso.winmd"]}
    # A tool is recorded as the command spelled it, often without `.exe`.
    assert packages["contoso.tool/1.2.3"]["files"] == {"lib": ["bin/tool.exe"]}
    assert packages["contoso.tool/1.2.3"]["cc"] == {}


def test_the_lock_says_which_native_target_uses_which_package():
    lock = nuget_assets.build_lock(_report(), REPO, read_assets=lambda p: None, verify=False)
    assert lock["packages"]["contoso.sdk/2.0.0"]["cc"]["include_dirs"] == ["include"]
    assert lock["packages"]["contoso.sdk/2.0.0"]["integrity"] == "sha512-AAAA"
    assert lock["targets"]["runner"] == {
        "cc": ["contoso.sdk/2.0.0"],
        "files": {"contoso.sdk/2.0.0": ["include/AutoInitializer.cpp", "metadata/Contoso.winmd"]}}
    # Headers only: no file is named, and a subset of the lock still has to
    # keep the package.
    assert lock["targets"]["lib"] == {"cc": ["contoso.sdk/2.0.0"],
                                      "files": {"contoso.tool/1.2.3": ["bin/tool.exe"]}}
    assert "contoso.build/1.0.0" not in lock["packages"]
    assert lock["sources"] == ["https://feed.example/v3/index.json"]


def test_optimization_options_are_reported_as_given():
    compiles = [Action(mnemonic="CppCompile", arguments=(
        "cl.exe", "/c", "/Zi", "/W4", "/O2", "/Oi", "/GL", "/D", "NDEBUG", "/Gy", "/EHsc", "a.cpp"))]
    link = Action(mnemonic="CppLink", arguments=(
        "link.exe", "/OUT:a.exe", "/OPT:REF", "/OPT:ICF", "/LTCG:incremental", "/LTCGOUT:a.iobj",
        "/DEBUG", "a.obj"))
    assert extract_msbuild._optimization(compiles, link) == {
        "compile": ["/O2", "/Oi", "/GL", "/Gy"], "link": ["/OPT:REF", "/OPT:ICF", "/LTCG:incremental"]}
    # A static library has no link; a Debug compile no optimization.
    assert extract_msbuild._optimization(compiles, None) == {"compile": ["/O2", "/Oi", "/GL", "/Gy"]}
    debug = [Action(mnemonic="CppCompile", arguments=("cl.exe", "/c", "/W4", "a.cpp"))]
    assert extract_msbuild._optimization(debug, None) == {}


def test_the_intermediate_compile_of_a_xaml_project_is_told_apart():
    def compile_to(out):
        return Action(mnemonic="CSharpCompile", arguments=("csc.exe", "/out:" + out, "A.cs"))
    assert extract_msbuild._is_xaml_precompile(compile_to(r"obj\Release\intermediatexaml\App.dll"))
    assert not extract_msbuild._is_xaml_precompile(compile_to(r"obj\Release\App.dll"))


class _NoTlogs:
    """A project group whose tracking logs are not on this machine."""
    dir = REPO + r"\src\app"
    name = "app"

    def prop(self, name):
        return ""

    def tasks(self, *names):
        return iter(())


def test_a_resource_script_is_reported_with_its_options():
    action = Action(mnemonic="ResourceCompile", cwd=REPO + r"\src\app", arguments=(
        "rc.exe", "/D", "_UNICODE", "/DAPP", "/l0x0409", "/I" + REPO + r"\src\common", "/nologo",
        r"/fox64\Release\app.res", "app.rc"))
    scripts = extract_msbuild._resource_scripts(
        _NoTlogs(), [action], Origins(REPO), REPO, extract_msbuild._TrueCase(False), ())
    assert scripts == [{"script": "src/app/app.rc", "defines": ["APP", "_UNICODE"],
                        "includes": ["src/common"], "options": ["/l0x0409"],
                        "output": "src/app/x64/Release/app.res", "reads": []}]


def test_the_hash_of_a_package_is_that_of_its_file():
    # `contentHash` leaves a signed package's signature out: no download matches it.
    with tempfile.TemporaryDirectory() as folder:
        root = os.path.join(folder, "contoso.sdk", "2.0.0")
        os.makedirs(root)
        with open(os.path.join(root, ".nupkg.metadata"), "w") as f:
            f.write('{"version": 2, "contentHash": "CONTENT==", "source": "https://feed.example/v3/index.json"}')
        with open(os.path.join(root, "contoso.sdk.2.0.0.nupkg.sha512"), "w") as f:
            f.write("FILE==\n")
        origins = Origins(REPO)
        package = origins.add_root(root, "nuget", "@nuget/contoso.sdk", "contoso.sdk", "2.0.0")
        assert origins.nuget_facts(package) == {
            "source": "https://feed.example/v3/index.json", "integrity": "sha512-FILE=="}
        # Without the file, the content hash is the last resort.
        os.remove(os.path.join(root, "contoso.sdk.2.0.0.nupkg.sha512"))
        assert origins.nuget_facts(package)["integrity"] == "sha512-CONTENT=="


def test_vcpkg_says_which_port_a_header_belongs_to():
    with tempfile.TemporaryDirectory() as installed:
        info = os.path.join(installed, "vcpkg", "info")
        os.makedirs(info)
        with open(os.path.join(info, "spdlog_1.8.5_x64-windows-static.list"), "w") as f:
            f.write("x64-windows-static/\nx64-windows-static/include/spdlog/spdlog.h\n"
                    "x64-windows-static/lib/spdlog.lib\n")
        with open(os.path.join(info, "vcpkg-cmake_2024-04-23_x64-windows.list"), "w") as f:
            f.write("x64-windows/share/vcpkg-cmake/vcpkg.cmake\n")
        owners = Origins(REPO).vcpkg_file_owners(installed)
        assert owners["x64-windows-static/include/spdlog/spdlog.h"] == "spdlog"
        assert owners["x64-windows/share/vcpkg-cmake/vcpkg.cmake"] == "vcpkg-cmake"
        assert "x64-windows-static/" not in owners               # directories are not files
        assert Origins(REPO).vcpkg_file_owners(os.path.join(installed, "absent")) == {}


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
