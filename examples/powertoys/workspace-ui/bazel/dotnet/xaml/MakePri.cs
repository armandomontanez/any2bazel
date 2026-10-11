// Builds a project's resource index (.pri) with makepri.exe, for pri.bzl.
//
//   make_pri <request.json>
//
// MSBuild's PRI targets write a configuration (priconfig.xml) with one index per
// kind of input, and for each index a list of files (*.resfiles):
//
//   layout     files of the project that are resources by being there (images),
//              named by their path below the project directory
//   resources  string tables (.resw)
//   pri        the indexes of libraries, merged into this one
//   embed      files stored IN the index: compiled XAML (.xbf), named by their
//              path below a directory of their own
//
// makepri names a resource after the file's path below the index root, so the
// inputs are laid out under one staging directory the way the project's were,
// and the configuration of the reference build is used with its paths replaced.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Xml.Linq;

static class MakePri
{
    static int Main(string[] args)
    {
        JsonNode request = JsonNode.Parse(File.ReadAllText(args[0]))!;
        string root = Directory.GetCurrentDirectory();
        string Absolute(string path) => Path.GetFullPath(Path.Combine(root, path));

        string stage = Absolute((string)request["stage"]!);
        if (Directory.Exists(stage))
        {
            // Copies of build outputs are read-only, which also stops a delete.
            foreach (string file in Directory.EnumerateFiles(stage, "*", SearchOption.AllDirectories))
                File.SetAttributes(file, FileAttributes.Normal);
            Directory.Delete(stage, recursive: true);
        }
        string lists = Path.Combine(stage, "_pri");
        string embedded = Path.Combine(lists, "embed");
        Directory.CreateDirectory(embedded);

        List<string> Lay(JsonNode files, string below)
        {
            var names = new List<string>();
            foreach (JsonNode? file in files.AsArray())
            {
                string name = ((string)file!["name"]!).Replace('/', '\\');
                string target = Path.Combine(below, name);
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                File.Copy(Absolute((string)file["path"]!), target, overwrite: true);
                File.SetAttributes(target, FileAttributes.Normal);
                names.Add(name);
            }
            return names;
        }

        File.WriteAllLines(Path.Combine(lists, "layout.resfiles"), Lay(request["layout"]!, stage));
        File.WriteAllLines(Path.Combine(embedded, "embed.resfiles"), Lay(request["embed"]!, embedded));
        File.WriteAllLines(Path.Combine(lists, "resources.resfiles"),
            request["resources"]!.AsArray().Select(p => Absolute((string)p!)));
        File.WriteAllLines(Path.Combine(lists, "pri.resfiles"),
            request["pris"]!.AsArray().Select(p => Absolute((string)p!)));

        XDocument config = XDocument.Load(Absolute((string)request["config"]!));
        foreach (XElement index in config.Root!.Elements("index"))
        {
            string list = Path.GetFileName(((string?)index.Attribute("startIndexAt") ?? "").Replace('/', '\\'));
            if (list.EndsWith("embed.resfiles", StringComparison.OrdinalIgnoreCase))
            {
                index.SetAttributeValue("root", @"_pri\embed");
                index.SetAttributeValue("startIndexAt", "embed.resfiles");
            }
            else if (list.EndsWith("layout.resfiles", StringComparison.OrdinalIgnoreCase))
            {
                index.SetAttributeValue("startIndexAt", @"_pri\layout.resfiles");
            }
            else if (list.Equals("resources.resfiles", StringComparison.OrdinalIgnoreCase) ||
                     list.Equals("pri.resfiles", StringComparison.OrdinalIgnoreCase))
            {
                index.SetAttributeValue("startIndexAt", @"_pri\" + list.ToLowerInvariant());
            }
            else
            {
                Console.Error.WriteLine($"make_pri: error: the configuration has an index this program does not know: {list}");
                return 1;
            }
        }
        string configPath = Path.Combine(lists, "priconfig.xml");
        config.Save(configPath);

        string output = Absolute((string)request["out"]!);
        var start = new ProcessStartInfo(Absolute((string)request["makepri"]!))
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (string argument in new[]
                 {
                     "new", "/pr", stage, "/cf", configPath, "/of", output,
                     "/in", (string)request["index_name"]!, "/o",
                 })
        {
            start.ArgumentList.Add(argument);
        }
        using Process process = Process.Start(start)!;
        string log = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
        process.WaitForExit();
        if (process.ExitCode != 0 || !File.Exists(output))
        {
            Console.Error.Write(log);
            Console.Error.WriteLine($"make_pri: makepri exited with {process.ExitCode}");
            return 1;
        }
        return 0;
    }
}
