// Runs the WPF markup compiler for xaml_wpf.bzl.
//
//   wpf_markup <request.json>
//
// WPF's markup compiler has no command line: it is three MSBuild task classes in
// PresentationBuildTasks.dll (of the .NET SDK's WindowsDesktop part).
//
//   MarkupCompilePass1   .xaml -> <Page>.g.cs, and <Page>.baml for markup that names
//                        no type of the project itself
//   MarkupCompilePass2   the rest of the .baml, once the project's types exist in a
//                        temporary assembly compiled from the pass 1 code
//   ResourcesGenerator   all .baml -> <Assembly>.g.resources, embedded in the assembly
//
// This program creates one of them, sets the properties MSBuild set (from a request
// with paths relative to the execution root), gives it a build engine that prints
// its diagnostics, and runs it from the project directory, where MSBuild ran it:
// markup is named by its path from there, and that name ends up in the program.
// Nothing here is compiled against MSBuild: the task and its interfaces are loaded
// from the SDK the request points into.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json.Nodes;

public class Engine : DispatchProxy
{
    public int Errors;

    protected override object? Invoke(MethodInfo? method, object?[]? args)
    {
        string name = method!.Name;
        if ((name == "LogErrorEvent" || name == "LogWarningEvent") && args![0] is object e)
        {
            Type t = e.GetType();
            object? Get(string property) => t.GetProperty(property)?.GetValue(e);
            if (name == "LogErrorEvent") Errors++;
            Console.Error.WriteLine("{0}({1}): {2} {3}: {4}", Get("File"), Get("LineNumber"),
                name == "LogErrorEvent" ? "error" : "warning", Get("Code"), Get("Message"));
        }
        Type result = method.ReturnType;
        if (result == typeof(void)) return null;
        if (result == typeof(string)) return "";
        return result.IsValueType ? Activator.CreateInstance(result) : null;
    }
}

static class WpfMarkup
{
    // A copy of a build output is read-only like its source; the tasks update
    // what they are given, and a second run has to replace it.
    static void CopyWritable(string source, string target)
    {
        if (File.Exists(target)) File.SetAttributes(target, FileAttributes.Normal);
        File.Copy(source, target, overwrite: true);
        File.SetAttributes(target, FileAttributes.Normal);
    }

    static int Main(string[] args)
    {
        JsonNode request = JsonNode.Parse(File.ReadAllText(args[0]))!;
        string root = Directory.GetCurrentDirectory();
        string Absolute(string path) => Path.GetFullPath(Path.Combine(root, path));

        string tasks = Absolute((string)request["tasks"]!);
        // The task's own dependencies sit beside it or in the SDK directory above.
        var probe = new List<string> { Path.GetDirectoryName(tasks)! };
        for (DirectoryInfo? d = new DirectoryInfo(probe[0]); d != null; d = d.Parent)
        {
            if (File.Exists(Path.Combine(d.FullName, "Microsoft.Build.Framework.dll"))) { probe.Add(d.FullName); break; }
        }
        AssemblyLoadContext.Default.Resolving += (context, name) =>
        {
            foreach (string directory in probe)
            {
                string candidate = Path.Combine(directory, name.Name + ".dll");
                if (File.Exists(candidate)) return context.LoadFromAssemblyPath(candidate);
            }
            return null;
        };

        string mode = (string)request["mode"]!;
        string output = Absolute((string)request["output"]!);
        Directory.CreateDirectory(output);
        string projectDirectory = Absolute((string)request["project_dir"]!);
        var inputs = new Dictionary<string, object>();
        foreach (var setting in request["settings"]!.AsObject()) inputs[setting.Key] = (string)setting.Value!;
        inputs["OutputPath"] = output + Path.DirectorySeparatorChar;

        List<(string spec, Dictionary<string, string> meta)> Items(string key, bool named) =>
            (request[key]?.AsArray() ?? new JsonArray()).Select(n => named
                ? ((string)n!["spec"]!, new Dictionary<string, string>())
                : (Absolute((string)n!), new Dictionary<string, string>())).ToList();

        string taskName;
        string workingDirectory = projectDirectory;
        if (mode == "pass1")
        {
            taskName = "MarkupCompilePass1";
            inputs["PageMarkup"] = Items("pages", named: true);
            inputs["ApplicationMarkup"] = Items("applications", named: true);
            // By item spec, like markup: the generated code records the path.
            inputs["ContentFiles"] = (request["content"]?.AsArray() ?? new JsonArray())
                .Select(n => ((string)n!, new Dictionary<string, string>())).ToList();
            inputs["SourceCodeFiles"] = Items("sources", named: false);
            inputs["References"] = Items("references", named: false);
        }
        else if (mode == "pass2")
        {
            taskName = "MarkupCompilePass2";
            // What pass 1 remembered: which markup is left, and what it was compiled against.
            foreach (JsonNode? state in request["state"]!.AsArray())
            {
                string file = Absolute((string)state!);
                CopyWritable(file, Path.Combine(output, Path.GetFileName(file)));
            }
            var references = Items("references", named: false);
            references.Add((Absolute((string)request["local_assembly"]!), new Dictionary<string, string>()));
            inputs["References"] = references;
        }
        else
        {
            taskName = "ResourcesGenerator";
            // A resource is named by its path below the output directory: lay the
            // compiled markup out under the names the reference build gave it.
            var files = new List<(string, Dictionary<string, string>)>();
            foreach (JsonNode? resource in request["resources"]!.AsArray())
            {
                string target = Path.Combine(output, ((string)resource!["name"]!).Replace('/', '\\'));
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                CopyWritable(Absolute((string)resource["path"]!), target);
                files.Add((target, new Dictionary<string, string>()));
            }
            inputs["ResourceFiles"] = files;
            inputs["OutputResourcesFile"] = new List<(string, Dictionary<string, string>)>
                { (Absolute((string)request["out"]!), new Dictionary<string, string>()) };
            workingDirectory = output;
        }

        Assembly assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(tasks);
        Type type = assembly.GetType("Microsoft.Build.Tasks.Windows." + taskName, throwOnError: true)!;
        // Before the task exists: it remembers the directory it was created in
        // as the project's.
        Directory.SetCurrentDirectory(workingDirectory);
        object task = Activator.CreateInstance(type)!;
        PropertyInfo engineProperty = type.GetProperty("BuildEngine")!;
        object engine = DispatchProxy.Create(engineProperty.PropertyType, typeof(Engine));
        engineProperty.SetValue(task, engine);
        Type itemInterface = engineProperty.PropertyType.Assembly.GetType("Microsoft.Build.Framework.ITaskItem", true)!;
        Type itemType = AssemblyLoadContext.Default
            .LoadFromAssemblyPath(Path.Combine(probe[^1], "Microsoft.Build.Utilities.Core.dll"))
            .GetType("Microsoft.Build.Utilities.TaskItem", true)!;

        Array TaskItems(List<(string spec, Dictionary<string, string> meta)> items)
        {
            Array array = Array.CreateInstance(itemInterface, items.Count);
            for (int i = 0; i < items.Count; i++)
            {
                object item = Activator.CreateInstance(itemType, items[i].spec)!;
                foreach (var m in items[i].meta) itemType.GetMethod("SetMetadata")!.Invoke(item, new object[] { m.Key, m.Value });
                array.SetValue(item, i);
            }
            return array;
        }

        foreach (var input in inputs)
        {
            PropertyInfo? property = type.GetProperty(input.Key);
            if (property == null || !property.CanWrite)
            {
                Console.Error.WriteLine($"wpf_markup: note: {taskName} has no settable property {input.Key}");
                continue;
            }
            Type target = property.PropertyType;
            object? value = input.Value switch
            {
                List<(string, Dictionary<string, string>)> list when target.IsArray => TaskItems(list),
                List<(string, Dictionary<string, string>)> list => list.Count > 0 ? TaskItems(list).GetValue(0) : null,
                string text when target == typeof(bool) => bool.Parse(text),
                string text when target == typeof(string) => text,
                string text when target == typeof(string[]) => text.Split(';', StringSplitOptions.RemoveEmptyEntries),
                string text when target.IsArray => TaskItems(text.Split(';', StringSplitOptions.RemoveEmptyEntries)
                    .Select(s => (s, new Dictionary<string, string>())).ToList()),
                string text => Convert.ChangeType(text, target),
                _ => null,
            };
            property.SetValue(task, value);
        }

        Directory.SetCurrentDirectory(workingDirectory);
        bool succeeded = (bool)type.GetMethod("Execute")!.Invoke(task, null)!;
        Directory.SetCurrentDirectory(root);
        int errors = ((Engine)engine).Errors;
        if (!succeeded || errors > 0)
        {
            Console.Error.WriteLine($"wpf_markup: {taskName} failed");
            return 1;
        }
        foreach (JsonNode? declared in request["outs"]!.AsArray())
        {
            if (!File.Exists(Absolute((string)declared!)))
            {
                Console.Error.WriteLine($"wpf_markup: error: {taskName} did not write {declared}");
                errors++;
            }
        }
        return errors == 0 ? 0 : 1;
    }
}
