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

"""Windows command-line tokenization. The command lines are the real shapes
MSBuild logs (taken from winget-cli and PowerToys builds), reduced."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from wincmdline import expand_response_files, split, split_tool


def test_quote_opening_mid_token_and_trailing_backslash():
    # `\\"` before the closing quote is ONE literal backslash, then the quote
    # closes. shlex split this into two tokens at the space.
    assert split(r'/I"x64\Release\Generated Files\\" /Zi') == [
        "/Ix64\\Release\\Generated Files\\", "/Zi"]


def test_backslashes_not_before_a_quote_are_literal():
    assert split(r'/analyze:rulesetdirectory";C:\Program Files (x86)\VS\\Rule Sets;" /FC') == [
        "/analyze:rulesetdirectory;C:\\Program Files (x86)\\VS\\\\Rule Sets;", "/FC"]


def test_odd_backslashes_before_quote_escape_it():
    assert split(r'a\"b "c\\\"d"') == ['a"b', 'c\\"d']


def test_doubled_quote_inside_quotes_is_a_literal_quote():
    assert split('"say ""hi"" now"') == ['say "hi" now']


def test_empty_quoted_argument_is_kept():
    assert split('a "" b') == ["a", "", "b"]


def test_newlines_separate_arguments():
    # LIB logs its object list one per line.
    assert split("/NOLOGO x64\\a.obj\r\nx64\\b.obj\n") == ["/NOLOGO", "x64\\a.obj", "x64\\b.obj"]


def test_single_quotes_are_not_quotes():
    assert split("""/MANIFESTUAC:"level='asInvoker' uiAccess='false'" /DLL""") == [
        "/MANIFESTUAC:level='asInvoker' uiAccess='false'", "/DLL"]


def test_unquoted_tool_path_with_spaces():
    tool, args = split_tool(
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC"
        r"\14.44.35207\bin\HostX64\x64\CL.exe /c /I..\ version.cpp")
    assert tool.endswith(r"\HostX64\x64\CL.exe")
    assert tool.startswith(r"C:\Program Files (x86)")
    assert args == ["/c", "/I..\\", "version.cpp"]


def test_quoted_tool_path():
    tool, args = split_tool(r'"C:\Program Files\VS\VC\vcpkg\vcpkg.exe" install --triplet "x64"')
    assert tool == r"C:\Program Files\VS\VC\vcpkg\vcpkg.exe"
    assert args == ["install", "--triplet", "x64"]


def test_bare_switch_list_has_no_tool():
    # Tracking logs store the switches without the tool.
    tool, args = split_tool('/c /I"C:\\A B\\\\" C:\\SRC\\X.CPP')
    assert tool == ""
    assert args == ["/c", "/IC:\\A B\\", "C:\\SRC\\X.CPP"]


def test_response_files_are_expanded_and_unreadable_ones_kept():
    files = {"C:\\p\\a.rsp": '-in "C:\\x y\\a.winmd"\n-out gen', "C:\\p\\nested.rsp": "@a.rsp -v"}
    resolve = lambda p: p if ":" in p else "C:\\p\\" + p
    args = expand_response_files(["tool.exe", "@nested.rsp", "@missing.rsp"],
                                 resolve=resolve, read=files.get)
    assert args == ["tool.exe", "-in", "C:\\x y\\a.winmd", "-out", "gen", "-v", "@missing.rsp"]


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
