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

"""Visual C++ tracking logs. Contents mirror real `*.tlog` files (UTF-16,
upper-cased paths, `^`-prefixed keys)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import msbuild_tlog as tlog

CL_COMMANDS = (
    "^C:\\P\\SRC\\LIB\\ICU\\SQLITEICU.C\r\n"
    '/c /I"C:\\P\\SRC\\LIB\\\\" /Zi /D NDEBUG /TC C:\\P\\SRC\\LIB\\ICU\\SQLITEICU.C\r\n'
    "^C:\\P\\SRC\\LIB\\PCH.CPP\r\n"
    '/c /I"C:\\P\\SRC\\LIB\\\\" /Zi /D NDEBUG /std:c++17 /Yc"pch.h" /TP C:\\P\\SRC\\LIB\\PCH.CPP\r\n'
)

LIB_COMMAND = (
    "^C:\\P\\SRC\\LIB\\X64\\A.OBJ|C:\\P\\SRC\\LIB\\X64\\B.OBJ\r\n"
    '/OUT:"C:\\P\\X64\\LIB.LIB" /NOLOGO X64\\A.OBJ\r\n'
    "X64\\B.OBJ\r\n"
)

LINK_READS = (
    "^C:\\P\\SRC\\DLL\\X64\\A.OBJ|C:\\P\\SRC\\DLL\\X64\\B.OBJ\r\n"
    "C:\\WINDOWS\\SYSTEM32\\TZRES.DLL\r\n"
    "C:\\PROGRAM FILES (X86)\\WINDOWS KITS\\10\\LIB\\10.0.26100.0\\UM\\X64\\KERNEL32.LIB\r\n"
    "C:\\VS\\VC\\TOOLS\\MSVC\\14.51.36231\\LIB\\SPECTRE\\X64\\MSVCRT.LIB\r\n"
)


def test_decode_handles_utf16_bom_and_utf8():
    assert tlog.decode("^A\r\nb".encode("utf-16")) == "^A\r\nb"
    assert tlog.decode(b"\xef\xbb\xbfx;y") == "x;y"
    assert tlog.decode(b"plain;text") == "plain;text"
    assert tlog.decode("ab".encode("utf-16-le")) == "ab"            # no BOM


def test_commands_are_keyed_by_upper_cased_source():
    commands = tlog.parse_commands(CL_COMMANDS)
    assert set(commands) == {"C:\\P\\SRC\\LIB\\ICU\\SQLITEICU.C", "C:\\P\\SRC\\LIB\\PCH.CPP"}
    assert "/TC" in commands["C:\\P\\SRC\\LIB\\ICU\\SQLITEICU.C"]
    assert '/Yc"pch.h"' in commands["C:\\P\\SRC\\LIB\\PCH.CPP"]     # switch case is preserved


def test_multi_source_key_and_multi_line_command():
    commands = tlog.parse_commands(LIB_COMMAND)
    # Found by any input, and the object list keeps its line breaks.
    assert commands["C:\\P\\SRC\\LIB\\X64\\B.OBJ"] == commands["C:\\P\\SRC\\LIB\\X64\\A.OBJ"]
    assert commands["C:\\P\\SRC\\LIB\\X64\\A.OBJ"].endswith("X64\\A.OBJ\nX64\\B.OBJ")
    assert "C:\\P\\SRC\\LIB\\X64\\A.OBJ|C:\\P\\SRC\\LIB\\X64\\B.OBJ" in commands


def test_read_log_lists_what_the_tool_opened():
    reads = tlog.parse_files(LINK_READS)
    files = reads["C:\\P\\SRC\\DLL\\X64\\A.OBJ"]
    assert files == reads["C:\\P\\SRC\\DLL\\X64\\B.OBJ"]
    # The Spectre-mitigated CRT is visible here and nowhere on a command line.
    assert any("\\SPECTRE\\X64\\MSVCRT.LIB" in f for f in files)


def test_items_log_restores_true_case():
    items = tlog.parse_items("C:\\p\\src\\lib\\Pch.cpp;C:\\p\\src\\lib\\x64\\Pch.obj\r\n")
    assert items == {"C:\\P\\SRC\\LIB\\PCH.CPP": "C:\\p\\src\\lib\\Pch.cpp"}


def test_lastbuildstate():
    state = tlog.parse_lastbuildstate(
        "PlatformToolSet=v145:VCToolArchitecture=Native64Bit:VCToolsVersion=14.51.36231:"
        "TargetPlatformVersion=10.0.26100.0:VcpkgTriplet=x64-release:\r\n"
        "Release|x64|C:\\p\\src\\|\r\n")
    assert state["PlatformToolSet"] == "v145"
    assert state["VCToolsVersion"] == "14.51.36231"
    assert (state["Configuration"], state["Platform"], state["SolutionDir"]) == (
        "Release", "x64", "C:\\p\\src\\")


def test_tlog_dir_reads_files_case_insensitively_and_tolerates_absence():
    with tempfile.TemporaryDirectory() as root:
        d = os.path.join(root, "AppInsta.F3F6E699.tlog")
        os.mkdir(d)
        # MSBuild versions disagree on the tool prefix's case.
        with open(os.path.join(d, "CL.command.1.tlog"), "wb") as f:
            f.write(CL_COMMANDS.encode("utf-16"))
        with open(os.path.join(d, "Lib.command.1.tlog"), "wb") as f:
            f.write(LIB_COMMAND.encode("utf-16"))
        with open(os.path.join(d, "Cl.items.tlog"), "wb") as f:
            f.write(b"C:\\p\\src\\lib\\pch.cpp;C:\\p\\src\\lib\\x64\\pch.obj\r\n")
        with open(os.path.join(d, "lib.lastbuildstate"), "wb") as f:
            f.write(b"PlatformToolSet=v143:\r\nDebug|x64|C:\\p\\|\r\n")
        t = tlog.TlogDir(d)
        assert t.exists()
        assert len(t.commands("cl")) == 2
        assert t.commands("lib")                     # found despite `Lib.` on disk
        assert t.commands("link") == {}              # no such file: empty, not an error
        assert t.items()["C:\\P\\SRC\\LIB\\PCH.CPP"].endswith("pch.cpp")
        assert t.lastbuildstate()["Configuration"] == "Debug"
        assert not t.failed_last_build()
        open(os.path.join(d, "unsuccessfulbuild"), "w").close()
        assert tlog.TlogDir(d).failed_last_build()

    missing = tlog.TlogDir(os.path.join(root, "gone.tlog"))
    assert not missing.exists()
    assert missing.commands("cl") == {} and missing.items() == {}


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
