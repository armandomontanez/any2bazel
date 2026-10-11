// Writes a program's host executable (`<Program>.exe`), for apphost.bzl.
//
//   apphost <Microsoft.NET.HostModel.dll> <apphost.exe template> <out.exe> <Program.dll name>
//           <gui|console> [<assembly to copy resources from>]
//
// A .NET program is a .dll; what the user starts is a copy of `apphost.exe` from
// the Microsoft.NETCore.App.Host pack with the .dll's name written into it, the
// Windows subsystem set and the assembly's icon and version resources copied
// over. MSBuild does that with HostWriter.CreateAppHost of the SDK's
// Microsoft.NET.HostModel.dll; so does this program, by calling the same method.
using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;

static class AppHost
{
    static int Main(string[] args)
    {
        string hostModel = Path.GetFullPath(args[0]);
        string template = Path.GetFullPath(args[1]);
        string output = Path.GetFullPath(args[2]);
        string binary = args[3];
        bool gui = args[4] == "gui";
        string? resources = args.Length > 5 ? Path.GetFullPath(args[5]) : null;

        string sdk = Path.GetDirectoryName(hostModel)!;
        AssemblyLoadContext.Default.Resolving += (context, name) =>
        {
            string candidate = Path.Combine(sdk, name.Name + ".dll");
            return File.Exists(candidate) ? context.LoadFromAssemblyPath(candidate) : null;
        };
        Type writer = AssemblyLoadContext.Default.LoadFromAssemblyPath(hostModel)
            .GetType("Microsoft.NET.HostModel.AppHost.HostWriter", throwOnError: true)!;
        MethodInfo create = writer.GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Where(m => m.Name == "CreateAppHost")
            .OrderByDescending(m => m.GetParameters().Length)
            .First();
        // The parameter list has grown over SDK versions: fill in by name and
        // leave the rest at their defaults.
        object?[] arguments = create.GetParameters().Select(p => p.Name switch
        {
            "appHostSourceFilePath" => template,
            "appHostDestinationFilePath" => output,
            "appBinaryFilePath" => binary,
            "windowsGraphicalUserInterface" => gui,
            "assemblyToCopyResourcesFrom" => (object?)resources,
            _ => p.HasDefaultValue ? p.DefaultValue : null,
        }).ToArray();
        try
        {
            create.Invoke(null, arguments);
        }
        catch (TargetInvocationException e)
        {
            Console.Error.WriteLine("apphost: error: " + e.InnerException);
            return 1;
        }
        return File.Exists(output) ? 0 : 1;
    }
}
