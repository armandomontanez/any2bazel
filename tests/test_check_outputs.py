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

"""check_outputs.py: what the two builds produced, compared by content."""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_outputs


def test_generated_code_is_compared_without_where_and_by_what_it_was_generated():
    winui = ('#pragma checksum "C:\\ref\\src\\app\\MainWindow.xaml" "{8829d00f}" "ABCD"\n'
             "namespace App\n{\n    partial class MainWindow { }\n}\n")
    bazel = winui.replace("C:\\ref\\src\\app", "src\\app")
    assert check_outputs.code_lines(winui) == check_outputs.code_lines(bazel)
    wpf = ('#pragma checksum "..\\..\\..\\App.xaml" "{ff18}" "12"\n'
           "//     Runtime Version:4.0.30319.42000\n"
           '        [System.CodeDom.Compiler.GeneratedCodeAttribute("PresentationBuildTasks", "10.0.12.0")]\n'
           '#line 5 "..\\..\\..\\App.xaml"\n        void InitializeComponent() { }\n')
    other = ('#pragma checksum "..\\..\\..\\..\\..\\..\\src\\app\\App.xaml" "{ff18}" "12"\n'
             '        [System.CodeDom.Compiler.GeneratedCodeAttribute("PresentationBuildTasks", "10.0.11.0")]\n'
             '#line 5 "..\\..\\..\\..\\..\\..\\src\\app\\App.xaml"\n        void InitializeComponent() { }\n')
    assert check_outputs.code_lines(wpf) == check_outputs.code_lines(other)
    # Anything else is a difference.
    assert check_outputs.code_lines(wpf) != check_outputs.code_lines(other.replace("void Init", "int Init"))


def test_each_xaml_output_is_looked_up_where_its_pass_wrote_it():
    winui = {"framework": "winui",
             "pass1": {"output_path": "src/app/obj/Release",
                       "outputs": {"code": ["src/app/obj/Release/Views/Main.g.i.cs",
                                            "src/app/obj/Release/Views/Main.g.cs",
                                            "src/app/obj/Release/XamlTypeInfo.g.cs"]}},
             "pass2": {"outputs": {"xaml": ["src/app/obj/Release/Views/Main.xaml"],
                                   "xbf": ["src/app/obj/Release/Views/Main.xbf"]}}}
    assert check_outputs.xaml_outputs(winui, "App") == [
        ("src/app/obj/Release/Views/Main.g.i.cs", "App_xaml1/Views/Main.g.i.cs"),
        # Pass 2 writes these again, complete.
        ("src/app/obj/Release/Views/Main.g.cs", "App_xaml2/Views/Main.g.cs"),
        ("src/app/obj/Release/XamlTypeInfo.g.cs", "App_xaml2/XamlTypeInfo.g.cs"),
        ("src/app/obj/Release/Views/Main.xaml", "App_xaml2/Views/Main.xaml"),
        ("src/app/obj/Release/Views/Main.xbf", "App_xaml2/Views/Main.xbf")]
    wpf = {"framework": "wpf",
           "pass1": {"output_path": "src/app/obj", "outputs": {"code": ["src/app/obj/App.g.cs"],
                                                               "baml": ["src/app/obj/Early.baml"]}},
           "pass2": {"outputs": {"baml": ["src/app/obj/App.baml"]}},
           "resources": {"output": "src/app/obj/App.g.resources"}}
    assert check_outputs.xaml_outputs(wpf, "App") == [
        ("src/app/obj/App.g.cs", "App_wpf1/App.g.cs"), ("src/app/obj/Early.baml", "App_wpf1/Early.baml"),
        ("src/app/obj/App.baml", "App_wpf2/App.baml"), ("src/app/obj/App.g.resources", "App.g.resources")]


def test_files_are_compared_by_kind():
    with tempfile.TemporaryDirectory() as root:
        def write(name, data):
            path = os.path.join(root, name)
            with open(path, "wb") as f:
                f.write(data)
            return path
        assert check_outputs.compare_file(write("a.xbf", b"\x01\x02"), write("b.xbf", b"\x01\x02")) is None
        assert "different bytes" in check_outputs.compare_file(write("c.xbf", b"\x01"), write("d.xbf", b"\x02"))
        a = write("a.g.cs", b'#pragma checksum "x"\nclass A { }\n')
        b = write("b.g.cs", b'#pragma checksum "y"\nclass A { }\n')
        assert check_outputs.compare_file(a, b) is None
        c = write("c.g.cs", b'#pragma checksum "y"\nclass B { }\n')
        assert check_outputs.compare_file(a, c) == "differs at line 1 of 1 (reference: 1 lines)"
        assert check_outputs.compare_file(os.path.join(root, "none.cs"), a) == "not built"


DUMP = """<PriInfo>
 <ResourceMap name="App">
  <ResourceMapSubtree name="Files">
   <NamedResource name="Main.xbf">
    <Candidate type="EmbeddedData"><QualifierSet/><Base64Value>%s</Base64Value></Candidate>
   </NamedResource>
   <ResourceMapSubtree name="Assets">
    <NamedResource name="Logo.png">
     <Candidate type="Path"><QualifierSet><Qualifier name="Scale" value="200"/></QualifierSet>
      <Value>Assets\\Logo.scale-200.png</Value></Candidate>
     <Candidate type="Path"><QualifierSet><Qualifier name="Scale" value="100"/></QualifierSet>
      <Value>Assets\\Logo.scale-100.png</Value></Candidate>
    </NamedResource>
   </ResourceMapSubtree>
  </ResourceMapSubtree>
  <ResourceMapSubtree name="Resources">
   <NamedResource name="Title"><Candidate type="String"><QualifierSet>
     <Qualifier name="Language" value="EN-US"/></QualifierSet><Value>%s</Value></Candidate></NamedResource>
  </ResourceMapSubtree>
 </ResourceMap>
</PriInfo>"""


def test_resource_indexes_are_compared_by_resource_and_candidate():
    mine = check_outputs.pri_resources(ET.fromstring(DUMP % ("AAAA", "Hosts")))
    assert sorted(mine) == ["App:Files/Assets/Logo.png", "App:Files/Main.xbf", "App:Resources/Title"]
    # Candidates in a fixed order, each with its qualifiers.
    assert mine["App:Files/Assets/Logo.png"] == [
        ("Scale=100", "Path", "Assets\\Logo.scale-100.png"),
        ("Scale=200", "Path", "Assets\\Logo.scale-200.png")]
    assert mine["App:Resources/Title"] == [("Language=EN-US", "String", "Hosts")]
    same = check_outputs.pri_resources(ET.fromstring(DUMP % ("AAAA", "Hosts")))
    assert check_outputs.compare_resources(mine, same) == {}
    other = check_outputs.pri_resources(ET.fromstring(DUMP % ("BBBB", "Host")))
    del other["App:Files/Assets/Logo.png"]
    assert check_outputs.compare_resources(mine, other) == {
        "only_here": ["App:Files/Assets/Logo.png"],
        "different": ["App:Files/Main.xbf", "App:Resources/Title"]}


EXPORTS = """    ordinal hint RVA      name

          1    0 00012340 DllCanUnloadNow
          2    1 00012350 powertoy_create = powertoy_create
"""
IMPORTS = """    KERNEL32.dll
             1400A1000 Import Address Table
             1400C2000 Import Name Table
                     0 time date stamp
                     0 Index of first forwarder reference

                         2B5 GetLastError
                         5F0 Sleep

    api-ms-win-core-file-l1-1-0.dll
             1400A1100 Import Address Table

                          1C CreateFileW
"""


def test_dumpbin_listings_are_read():
    assert check_outputs.parse_exports(EXPORTS.splitlines()) == {"DllCanUnloadNow", "powertoy_create"}
    assert check_outputs.parse_imports(IMPORTS.splitlines()) == {
        "kernel32.dll": {"GetLastError", "Sleep"}, "api-ms-win-core-file-l1-1-0.dll": {"CreateFileW"}}


def test_a_function_imported_from_another_dll_is_a_note_not_a_difference():
    reference = {"exports": {"f"}, "resources": {"VERSION": 1},
                 "imports": {"kernel32.dll": {"Sleep"}, "api-ms-win-core-file-l1-1-0.dll": {"CreateFileW"}}}
    moved = dict(reference, imports={"kernel32.dll": {"Sleep", "CreateFileW"}})
    differences, notes = check_outputs.compare_binaries(moved, reference)
    assert differences == [] and len(notes) == 1 and notes[0].startswith("1 functions are imported")
    assert check_outputs.compare_binaries(reference, reference) == ([], [])
    # Another function, a missing export or a missing resource is a difference.
    other = dict(reference, exports=set(), resources={},
                 imports={"kernel32.dll": {"Sleep", "CreateFileA"}})
    differences, notes = check_outputs.compare_binaries(other, reference)
    assert notes == [] and len(differences) == 3
    assert differences[0].startswith("exports: 0 only here [], 1 only in the reference ['f']")
    assert "CreateFileA" in differences[1] and "CreateFileW" in differences[1]
    assert differences[2] == "resources: {} here, {'VERSION': 1} in the reference"


def test_the_listing_says_same_or_different():
    text, failed = check_outputs.render({
        "xaml": [{"target": "App", "kind": "winui", "files": 5, "differences": {}}],
        "binaries": [{"target": "Mod", "exports": 1, "imports": 8, "differences": [],
                      "notes": ["2 functions are imported from another DLL"]}]})
    assert not failed
    assert "xaml: 1 compared, 0 differ" in text and "  same App (5 files)" in text
    assert "note: 2 functions" in text
    text, failed = check_outputs.render({
        "pri": [{"target": "App", "resources": 3, "differences": {"only_here": ["a", "b"]}}]})
    assert failed and "  DIFF App (3 resources)" in text and "only_here: 2: ['a', 'b']" in text


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
