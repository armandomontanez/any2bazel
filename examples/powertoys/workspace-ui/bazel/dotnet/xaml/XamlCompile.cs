// Runs the WinUI XAML compiler (XamlCompiler.exe of the Microsoft.WindowsAppSDK.WinUI
// package) for xaml_winui.bzl.
//
//   xaml_compile <request.json>
//
// MSBuild runs the compiler as a task (CompileXaml) with some thirty parameters. The
// executable takes the same parameters as a JSON file, and answers with another:
// the files it wrote and its diagnostics. Its exit code says nothing about the
// diagnostics, and it wants absolute paths, which only exist once the action runs.
// So this program writes the input from a request with paths relative to the
// execution root, runs the compiler, prints the diagnostics like a compiler would
// and fails when one is an error or a file the rule declared is missing.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;

static class XamlCompile
{
    // Parameters of the compiler that are flags; every other scalar is a string.
    static readonly string[] Flags =
    {
        "ForceSharedStateShutdown", "DisableXbfGeneration", "DisableXbfLineInfo", "EnableXBindDiagnostics",
        "IsPass1", "XAMLFingerprint", "UseVCMetaManaged", "IgnoreSpecifiedTargetPlatformMinVersion",
    };

    // All its string parameters: one it is not given must be present as null.
    static readonly string[] Strings =
    {
        "Language", "LanguageSourceExtension", "TargetPlatformMinVersion", "BuildConfiguration",
        "CIncludeDirectories", "ProjectName", "RootNamespace", "OutputType", "PriIndexName",
        "CodeGenerationControlFlags", "FeatureControlFlags", "VCInstallDir", "VCInstallPath32",
        "VCInstallPath64", "WindowsSdkPath", "CompileMode", "RootsLog", "SuppressWarnings",
        "PrecompiledHeaderFile", "XamlResourceMapName", "XamlComponentResourceLocation", "XamlPlatform",
        "TargetFileName",
    };

    static int Main(string[] args)
    {
        JsonNode request = JsonNode.Parse(File.ReadAllText(args[0]))!;
        string root = Directory.GetCurrentDirectory();
        string Absolute(string path) => Path.GetFullPath(Path.Combine(root, path));

        string output = Absolute((string)request["output"]!);
        Directory.CreateDirectory(output);
        var settings = request["settings"]!.AsObject().ToDictionary(p => p.Key, p => (string)p.Value!);
        var notFromPackages = new HashSet<string>(
            request["not_from_packages"]!.AsArray().Select(n => (string)n!), StringComparer.OrdinalIgnoreCase);

        var input = new JsonObject();
        foreach (string name in Strings) input[name] = settings.TryGetValue(name, out string? value) ? value : null;
        foreach (string name in Flags)
            input[name] = settings.TryGetValue(name, out string? value) && bool.Parse(value);
        foreach (string name in settings.Keys.Except(Strings).Except(Flags))
            Console.Error.WriteLine($"xaml_compile: note: setting {name} is not a parameter of the compiler");

        input["ProjectPath"] = Absolute((string)request["project"]!);
        input["OutputPath"] = output + Path.DirectorySeparatorChar;
        // Where the compiler keeps what it knows from its last run. Without one it
        // reports failure after writing everything.
        input["SavedStateFile"] = Path.Combine(output, "XamlSaveStateFile.xml");
        input["GenXbfPath"] = Absolute((string)request["gen_xbf"]!) + Path.DirectorySeparatorChar;
        input["FingerprintIgnorePaths"] = null;

        input["XamlPages"] = Pages(request["pages"]!.AsArray(), Absolute);
        input["XamlApplications"] = Pages(request["applications"]!.AsArray(), Absolute);
        input["SdkXamlPages"] = null;
        input["ClIncludeFiles"] = null;
        // Walked without a check, unlike the other lists.
        input["ReferenceAssemblyPaths"] = new JsonArray();
        var references = new JsonArray();
        foreach (JsonNode? reference in request["references"]!.AsArray())
        {
            string path = Absolute((string)reference!);
            references.Add(Item(path, path, !notFromPackages.Contains(Path.GetFileName(path)), ""));
        }
        input["ReferenceAssemblies"] = references;
        string local = (string?)request["local_assembly"] ?? "";
        // Null, not empty, when there is none: pass 1 indexes into the list.
        input["LocalAssembly"] = local == "" ? null : new JsonArray(Item(Absolute(local), Absolute(local), false, ""));

        string inputPath = Path.Combine(output, "input.json");
        string outputPath = Path.Combine(output, "output.json");
        File.WriteAllText(inputPath, input.ToJsonString());
        File.Delete(outputPath);

        var start = new ProcessStartInfo(Absolute((string)request["compiler"]!))
        {
            // As under MSBuild: pass 2 opens each page by its item spec, from
            // the current directory.
            WorkingDirectory = Path.GetDirectoryName(Absolute((string)request["project"]!))!,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        start.ArgumentList.Add(inputPath);
        start.ArgumentList.Add(outputPath);
        using Process process = Process.Start(start)!;
        string stdout = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
        process.WaitForExit();

        int errors = 0;
        if (File.Exists(outputPath))
        {
            JsonNode result = JsonNode.Parse(File.ReadAllText(outputPath))!;
            foreach (JsonNode? entry in result["MSBuildLogEntries"]?.AsArray() ?? new JsonArray())
            {
                // 0 is a progress message; 1 a warning; 2 an error.
                int type = (int)entry!["Type"]!;
                if (type == 0) continue;
                if (type == 2) errors++;
                string file = (string?)entry["File"] ?? "";
                Console.Error.WriteLine("{0}{1}: {2} {3}: {4}",
                    file.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                        ? file.Substring(root.Length + 1) : file,
                    (int)entry["LineNumber"]! > 0 ? $"({entry["LineNumber"]},{entry["ColumnNumber"]})" : "",
                    type == 2 ? "error" : "warning", (string?)entry["ErrorCode"], (string?)entry["Message"]);
            }
        }
        else
        {
            Console.Error.WriteLine("xaml_compile: the compiler wrote no result file");
            errors++;
        }
        if (errors > 0 || process.ExitCode != 0)
        {
            Console.Error.Write(stdout);
            if (errors == 0) Console.Error.WriteLine($"xaml_compile: the compiler exited with {process.ExitCode}");
            return 1;
        }

        foreach (JsonNode? declared in request["outs"]!.AsArray())
        {
            string path = Absolute((string)declared!);
            if (!File.Exists(path))
            {
                Console.Error.WriteLine($"xaml_compile: error: the compiler did not write {declared}");
                errors++;
            }
            else if (path.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
            {
                Relocate(path, root);
            }
        }
        return errors == 0 ? 0 : 1;
    }

    static JsonArray? Pages(JsonArray pages, Func<string, string> absolute)
    {
        if (pages.Count == 0) return null;
        var items = new JsonArray();
        foreach (JsonNode? page in pages)
            items.Add(Item((string)page!["spec"]!, absolute((string)page["path"]!), false, (string?)page["link"] ?? ""));
        return items;
    }

    static JsonNode Item(string spec, string fullPath, bool fromPackage, string link) => new JsonObject
    {
        ["DependentUpon"] = "",
        ["FullPath"] = fullPath,
        ["ItemSpec"] = spec,
        ["IsSystemReference"] = false,
        ["IsNuGetReference"] = fromPackage,
        ["IsStaticLibraryReference"] = false,
        ["MSBuild_Link"] = link,
        ["MSBuild_TargetPath"] = "",
        ["MSBuild_XamlResourceMapName"] = "",
        ["MSBuild_XamlComponentResourceLocation"] = "",
    };

    // `#pragma checksum "<absolute path of the .xaml>"` is the one place the compiler
    // writes where it ran. Make it the path from the execution root, so the file is
    // the same on every machine.
    static void Relocate(string path, string root)
    {
        string text = File.ReadAllText(path);
        string prefix = "#pragma checksum \"" + root + Path.DirectorySeparatorChar;
        if (!text.Contains(prefix, StringComparison.OrdinalIgnoreCase)) return;
        var encoding = new System.Text.UTF8Encoding(encoderShouldEmitUTF8Identifier: HasPreamble(path));
        File.WriteAllText(path, text.Replace(prefix, "#pragma checksum \"", StringComparison.OrdinalIgnoreCase), encoding);
    }

    static bool HasPreamble(string path)
    {
        using FileStream stream = File.OpenRead(path);
        return stream.ReadByte() == 0xEF && stream.ReadByte() == 0xBB && stream.ReadByte() == 0xBF;
    }
}
