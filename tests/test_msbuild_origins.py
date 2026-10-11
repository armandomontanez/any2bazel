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

"""Classifying where a path in an MSBuild build comes from. Roots and layouts
are those of real winget-cli / PowerToys builds."""

import base64
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import msbuild_origins as origins
from msbuild_events import _Props
from msbuild_origins import Origins

REPO = "C:\\work\\proj"

PROPS = _Props({
    "MSBuildProjectFullPath": "C:\\work\\proj\\src\\lib\\lib.vcxproj",
    "ProjectDir": "C:\\work\\proj\\src\\lib\\",
    "IntDir": "x64\\Release\\",
    "OutDir": "C:\\work\\proj\\x64\\Release\\lib\\",
    "VCToolsInstallDir": "C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\",
    "VCToolsVersion": "14.44.35207",
    "VCInstallDir": "C:\\VS\\VC\\",
    "VSInstallDir": "C:\\VS\\",
    "WindowsSdkDir": "C:\\Program Files (x86)\\Windows Kits\\10\\",
    "WindowsTargetPlatformVersion": "10.0.26100.0",
    "NuGetPackageRoot": "C:\\Users\\me\\.nuget\\packages\\",
    "VcpkgInstalledDir": "C:\\work\\proj\\vcpkg_installed\\x64\\",
    "VcpkgTriplet": "x64-windows-static",
    "_ZVcpkgCurrentInstalledDir": "C:\\work\\proj\\vcpkg_installed\\x64\\x64-windows-static\\",
    "NetCoreTargetingPackRoot": "C:\\Program Files\\dotnet\\packs",
})


def _origins(tracked=("src/lib/a.cpp", "src/lib/a.h")):
    o = Origins(REPO)
    o.learn(PROPS)
    o.set_tracked(tracked)
    return o


def test_toolchain_roots_come_from_the_build_properties():
    o = _origins()
    c = o.classify("C:\\VS\\VC\\Tools\\MSVC\\14.44.35207\\include\\vector")
    assert (c.kind, c.path, c.root.version) == ("msvc", "@msvc/include/vector", "14.44.35207")
    # The versioned toolset is the more specific root; the rest of VC\ is @vc.
    assert o.classify("C:\\VS\\VC\\Auxiliary\\VS\\UnitTest\\include").path \
        == "@vc/Auxiliary/VS/UnitTest/include"
    c = o.classify("C:\\PROGRAM FILES (X86)\\WINDOWS KITS\\10\\Lib\\10.0.26100.0\\um\\x64\\kernel32.lib")
    assert (c.kind, c.root.version) == ("winsdk", "10.0.26100.0")
    assert c.path == "@winsdk/Lib/10.0.26100.0/um/x64/kernel32.lib"
    assert o.classify("C:\\Program Files\\dotnet\\packs\\Microsoft.NETCore.App.Ref\\8.0.1").kind \
        == "dotnet"


def test_nuget_packages_config_directory():
    o = _origins()
    c = o.classify("C:\\work\\proj\\packages\\Microsoft.Windows.ImplementationLibrary.1.0.250325.1"
                   "\\build\\native\\..\\..\\include\\wil\\resource.h")
    assert c.kind == "nuget"
    assert c.path == "@nuget/microsoft.windows.implementationlibrary/include/wil/resource.h"
    assert (c.root.id, c.root.version) == ("Microsoft.Windows.ImplementationLibrary",
                                           "1.0.250325.1")
    # A prerelease version, and a `packages` directory that is not at the repo root.
    c = o.classify("C:\\work\\proj\\installer\\packages\\robmikh.common.0.0.23-beta\\include")
    assert (c.root.id, c.root.version) == ("robmikh.common", "0.0.23-beta")


def test_nuget_global_packages_folder():
    c = _origins().classify("C:\\Users\\me\\.nuget\\packages\\microsoft.windowsappsdk\\2.2.0"
                            "\\include\\MddBootstrap.h")
    assert c.path == "@nuget/microsoft.windowsappsdk/include/MddBootstrap.h"
    assert (c.root.id, c.root.version) == ("microsoft.windowsappsdk", "2.2.0")


def test_vcpkg_tree_inside_the_repo_is_vcpkg_not_repo():
    c = _origins().classify("C:\\work\\proj\\vcpkg_installed\\x64\\x64-windows-static\\include\\spdlog")
    assert (c.kind, c.path) == ("vcpkg", "@vcpkg/x64-windows-static/include/spdlog")


def test_repo_files_tracked_generated_and_build_dirs():
    o = _origins()
    assert o.classify("C:\\work\\proj\\src\\lib\\a.cpp").kind == "repo"
    assert o.classify("C:\\WORK\\PROJ\\SRC\\LIB\\A.CPP").path == "SRC/LIB/A.CPP"
    # Not tracked by git: something produced it (MIDL output written to the source tree).
    assert o.classify("C:\\work\\proj\\src\\server\\Server_c.c").kind == "generated"
    # Under a project's intermediate directory: generated even if git is unknown.
    o.set_tracked(None)
    assert o.classify("C:\\work\\proj\\src\\lib\\x64\\Release\\Generated Files\\m.g.cpp").kind \
        == "generated"
    assert o.classify("C:\\work\\proj\\src\\server\\Server_c.c").kind == "repo"   # cannot tell


def test_system_and_unknown():
    o = _origins()
    assert o.classify("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe").kind \
        == "system"
    # Nothing the build declared: an undeclared host dependency, surfaced as such.
    c = o.classify("D:\\thirdparty\\boost\\include")
    assert (c.kind, c.path) == ("unknown", "D:/thirdparty/boost/include")
    assert o.classify("relative\\path").kind == "unknown"


def test_a_root_that_contains_the_repo_is_ignored():
    o = Origins(REPO)
    o.learn(_Props({"VSInstallDir": "C:\\"}))       # a nonsense property value
    assert o.classify("C:\\work\\proj\\src\\a.cpp").kind == "repo"


def test_usage_is_recorded_per_root_and_feeds_the_path_map():
    o = _origins()
    wil = "C:\\work\\proj\\packages\\Microsoft.Windows.ImplementationLibrary.1.0.250325.1"
    o.note(wil + "\\include", "include", "lib")
    o.note(wil + "\\build\\native\\wil.targets", "import", "lib")
    # Only imported build logic: nothing a compile or link line could reference.
    o.note("C:\\Users\\me\\.nuget\\packages\\stylecop.analyzers\\1.1.118\\build\\x.props",
           "import", "tool")
    root = next(r for r in o.roots() if r.id == "Microsoft.Windows.ImplementationLibrary")
    assert root.include_dirs == {"include"}
    assert root.imports == {"build/native/wil.targets"}
    assert root.consumers == {"lib"}
    tokens = dict((token, path) for path, token in o.path_map())
    assert tokens["@nuget/microsoft.windows.implementationlibrary"] == wil
    assert "@nuget/stylecop.analyzers" not in tokens
    assert "@msvc" in tokens and "@winsdk" in tokens
    # A package that only has its consumers compile a source is used by a
    # compile: the source is spelled with the package's token.
    ml = "C:\\Users\\me\\.nuget\\packages\\contoso.ml\\2.1.70"
    o.note(ml + "\\include\\AutoInitializer.cpp", "source", "app")
    assert next(r for r in o.roots() if r.id == "contoso.ml").sources == {"include/AutoInitializer.cpp"}
    assert dict((token, path) for path, token in o.path_map())["@nuget/contoso.ml"] == ml
    # Longest path first, so the versioned toolset wins over the VC directory.
    paths = [p for p, _ in o.path_map()]
    assert paths.index("C:\\VS\\VC\\Tools\\MSVC\\14.44.35207") < paths.index("C:\\VS\\VC")


def test_nuget_facts_from_the_global_folder_metadata():
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, ".nupkg.metadata"), "w", encoding="utf-8") as f:
            json.dump({"version": 2, "contentHash": "QUJD",
                       "source": "https://feed.example/v3/index.json"}, f)
        facts = Origins(REPO).nuget_facts(origins.Root(root, "nuget", "@nuget/x"))
        assert facts == {"integrity": "sha512-QUJD",
                         "source": "https://feed.example/v3/index.json"}


def test_nuget_facts_hash_the_package_when_only_the_nupkg_is_there():
    # packages.config restore keeps the .nupkg and no metadata file.
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "Foo.1.2.3.nupkg"), "wb") as f:
            f.write(b"not really a zip")
        facts = Origins(REPO).nuget_facts(origins.Root(root, "nuget", "@nuget/foo"))
        expected = base64.b64encode(hashlib.sha512(b"not really a zip").digest()).decode()
        assert facts == {"integrity": "sha512-" + expected}
        assert "source" not in facts                 # unknown stays unknown


def test_nuget_facts_absent_tree():
    assert Origins(REPO).nuget_facts(origins.Root("Z:\\nope", "nuget", "@nuget/x")) == {}


def test_vcpkg_ports_from_its_own_database():
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "vcpkg", "info"))
        with open(os.path.join(root, "vcpkg", "status"), "w", encoding="utf-8") as f:
            f.write("Package: curl\nVersion: 8.19.0\nPort-Version: 0\nArchitecture: x64-release\n"
                    "Status: install ok installed\n\n"
                    "Package: curl\nFeature: ssl\nArchitecture: x64-release\n"
                    "Status: install ok installed\n\n"
                    "Package: detours\nVersion: 4.0.1\nPort-Version: 8\n"
                    "Architecture: x64-release\nStatus: install ok installed\n\n"
                    "Package: gone\nVersion: 1\nArchitecture: x64-release\n"
                    "Status: purge ok not-installed\n")
        with open(os.path.join(root, "vcpkg", "info", "curl_8.19.0_x64-release.list"), "w") as f:
            f.write("x64-release/\nx64-release/include/curl/curl.h\nx64-release/lib/libcurl.lib\n"
                    "x64-release/debug/lib/libcurl-d.lib\n")
        ports = Origins(REPO).vcpkg_ports(root)
        assert [(p["name"], p["version"]) for p in ports] == [("curl", "8.19.0"),
                                                             ("detours", "4.0.1#8")]
        assert ports[0]["features"] == ["ssl"]
        assert ports[0]["libs"] == ["libcurl.lib"]   # release libraries only
    assert Origins(REPO).vcpkg_ports("Z:\\nope") == []


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
