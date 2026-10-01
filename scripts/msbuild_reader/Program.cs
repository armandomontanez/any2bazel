using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Microsoft.Build.Logging.StructuredLogger;

namespace BinlogToJson
{
    class Program
    {
        static void Main(string[] args)
        {
            if (args.Length < 2)
            {
                Console.WriteLine("Usage: BinlogToJson <msbuild.binlog> <out.json>");
                return;
            }

            var binlogPath = args[0];
            var outPath = args[1];

            var build = Serialization.Read(binlogPath);

            var projects = new List<ProjectData>();

            build.VisitAllChildren<Microsoft.Build.Logging.StructuredLogger.Task>(task =>
            {
                var taskName = task.Name;
                var outTaskName = taskName;
                if (taskName == "CL") outTaskName = "ClCompile";
                if (taskName == "LIB") outTaskName = "Link";

                if (outTaskName == "ClCompile" || outTaskName == "Link" || outTaskName == "Csc")
                {
                    var proj = task.GetNearestParent<Project>();
                    if (proj != null)
                    {
                        var pData = projects.Find(p => p.ProjectFile == proj.ProjectFile);
                        if (pData == null)
                        {
                            pData = new ProjectData { Name = proj.Name, ProjectFile = proj.ProjectFile };
                            projects.Add(pData);
                        }
                        var tData = new TaskData { Name = outTaskName, CommandLineArguments = task.CommandLineArguments };
                        if (!string.IsNullOrEmpty(tData.CommandLineArguments))
                        {
                            pData.Tasks.Add(tData);
                        }
                    }
                }
            });

            bool incompleteBinlogDetected = false;
            build.VisitAllChildren<Target>(target =>
            {
                if (target.Name == "CoreCompile" || target.Name == "ClCompile")
                {
                    bool hasCompilerTask = target.HasChildren && target.Children.Any(c =>
                        c is Microsoft.Build.Logging.StructuredLogger.Task t &&
                        (t.Name == "Csc" || t.Name == "CL" || t.Name == "ClCompile")
                    );

                    // If a compilation target ran but didn't execute the actual compiler task,
                    // it was likely skipped due to incremental build up-to-date checks.
                    if (!hasCompilerTask)
                    {
                        var proj = target.GetNearestParent<Project>();
                        if (proj != null)
                        {
                            Console.Error.WriteLine($"[WARNING] Target '{target.Name}' in project '{proj.Name}' did not execute a compiler task. This indicates an incremental build where targets were skipped.");
                            incompleteBinlogDetected = true;
                        }
                    }
                }
            });

            if (incompleteBinlogDetected)
            {
                Console.Error.WriteLine("\n[ERROR] The provided binary log is incomplete because it was generated during an incremental build.");
                Console.Error.WriteLine("Compiler command line arguments are not evaluated for skipped targets. You MUST generate the binlog using a full rebuild (e.g., msbuild -t:Rebuild -bl) for accurate Bazel migration.");
                // We don't exit, so that projects that legitimately have no inputs (like packaging projects) don't block the extraction.
                // The missing targets will correctly be flagged by the diffing tool later.
            }

            var json = JsonSerializer.Serialize(projects, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(outPath, json);
        }
    }

    class ProjectData
    {
        public string Name { get; set; }
        public string ProjectFile { get; set; }
        public List<TaskData> Tasks { get; set; } = new List<TaskData>();
    }

    class TaskData
    {
        public string Name { get; set; }
        public string CommandLineArguments { get; set; }
    }
}
