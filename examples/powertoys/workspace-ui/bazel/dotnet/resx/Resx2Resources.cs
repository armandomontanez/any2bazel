// Converts a .resx file into the binary .resources format the C# compiler
// embeds: what MSBuild's GenerateResource task does for an SDK-style project.
//
// The cases and their order follow MSBuild's own reader (MSBuildResXReader) and
// it writes with the same writer (PreserializedResourceWriter), so that a
// resource is stored the way the reference build stored it:
//
//   no type                  a string
//   ResXFileRef              the linked file: text, bytes, a stream, or an
//                            object the run time constructs from the stream
//   a type, no mimetype      a primitive, or a string its TypeConverter reads
//   bytearray.base64         bytes for the type's TypeConverter
//   binary.base64            a BinaryFormatter payload, passed through
//
// Nothing is deserialized here: typed entries are stored as written, with the
// type's name, and become objects when the program reads them.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Resources.Extensions;
using System.Text;
using System.Xml;
using System.Xml.Linq;

internal static class Program
{
    private const string ByteArrayMime = "application/x-microsoft.net.object.bytearray.base64";
    private const string BinaryMime = "application/x-microsoft.net.object.binary.base64";

    private static int Main(string[] args)
    {
        if (args.Length != 2)
        {
            Console.Error.WriteLine("usage: resx2resources <in.resx> <out.resources>");
            return 2;
        }

        string resx = args[0];
        string directory = Path.GetDirectoryName(Path.GetFullPath(resx));

        // With the reader MSBuild uses. XmlTextReader does not normalize line
        // ends, so a multi-line string keeps the \r\n it has in the file; the
        // default reader would store \n and every such resource would differ.
        XDocument document;
        using (var stream = File.OpenRead(resx))
        using (var reader = new XmlTextReader(stream) { WhitespaceHandling = WhitespaceHandling.None })
        {
            document = XDocument.Load(reader, LoadOptions.PreserveWhitespace);
        }


        // <assembly alias="System.Drawing" name="System.Drawing, Version=..."/>
        var aliases = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (XElement assembly in document.Root.Elements("assembly"))
        {
            string alias = (string)assembly.Attribute("alias");
            string name = (string)assembly.Attribute("name");
            if (alias != null && name != null)
            {
                aliases[alias] = name;
            }
        }

        try
        {
            using (var writer = new PreserializedResourceWriter(args[1]))
            {
                foreach (XElement data in document.Root.Elements("data"))
                {
                    Add(writer, data, aliases, directory);
                }

                writer.Generate();
            }
        }
        catch (Exception e) when (e is FormatException || e is IOException || e is NotSupportedException)
        {
            Console.Error.WriteLine($"{resx}: {e.Message}");
            return 1;
        }

        return 0;
    }

    private static void Add(PreserializedResourceWriter writer, XElement data, Dictionary<string, string> aliases, string directory)
    {
        string name = (string)data.Attribute("name");
        string value = (string)data.Element("value");
        string type = (string)data.Attribute("type");
        string mime = (string)data.Attribute("mimetype");

        if (type == null && mime == null)
        {
            writer.AddResource(name, value ?? string.Empty);
            return;
        }

        type = FullName(type, aliases);
        if (type != null && type.StartsWith("System.Resources.ResXNullRef", StringComparison.Ordinal))
        {
            writer.AddResource(name, (object)null);
            return;
        }

        if (type != null && type.StartsWith("System.Resources.ResXFileRef", StringComparison.Ordinal))
        {
            AddLinked(writer, name, value, aliases, directory);
            return;
        }

        if (mime == null)
        {
            if (IsType(type, "System.String"))
            {
                writer.AddResource(name, value ?? string.Empty);
            }
            else if (TryPrimitive(type, value, out object primitive))
            {
                writer.AddResource(name, primitive);
            }
            else
            {
                writer.AddResource(name, value, type);      // read by the type's TypeConverter
            }

            return;
        }

        byte[] bytes = Convert.FromBase64String(value ?? string.Empty);
        switch (mime)
        {
            case ByteArrayMime:
                if (IsType(type, "System.Byte[]"))
                {
                    writer.AddResource(name, bytes);
                }
                else if (IsType(type, "System.IO.MemoryStream"))
                {
                    writer.AddResource(name, new MemoryStream(bytes), closeAfterWrite: true);
                }
                else
                {
                    writer.AddTypeConverterResource(name, bytes, type);
                }

                return;
            case BinaryMime:
                writer.AddBinaryFormattedResource(name, bytes);
                return;
            default:
                throw new NotSupportedException($"resource '{name}' has mimetype '{mime}', which is not supported");
        }
    }

    // value: "<path>;<type name>[;<encoding>]", the path relative to the .resx.
    private static void AddLinked(PreserializedResourceWriter writer, string name, string value, Dictionary<string, string> aliases, string directory)
    {
        string[] parts = SplitFileRef(value ?? string.Empty);
        if (parts.Length < 2)
        {
            throw new FormatException($"resource '{name}' is a file reference without a type: '{value}'");
        }

        string file = Path.Combine(directory, parts[0].Replace('\\', Path.DirectorySeparatorChar));
        string type = FullName(parts[1], aliases);
        if (IsType(type, "System.String"))
        {
            Encoding encoding = parts.Length > 2 ? Encoding.GetEncoding(parts[2]) : Encoding.Default;
            using (var reader = new StreamReader(file, encoding, detectEncodingFromByteOrderMarks: true))
            {
                writer.AddResource(name, reader.ReadToEnd());
            }
        }
        else if (IsType(type, "System.Byte[]"))
        {
            writer.AddResource(name, File.ReadAllBytes(file));
        }
        else if (IsType(type, "System.IO.MemoryStream"))
        {
            writer.AddResource(name, new MemoryStream(File.ReadAllBytes(file)), closeAfterWrite: true);
        }
        else
        {
            // An icon, a bitmap: constructed from the file's bytes at run time.
            writer.AddActivatorResource(name, File.OpenRead(file), type, closeAfterWrite: true);
        }
    }

    private static string[] SplitFileRef(string value)
    {
        // The path may be quoted and may itself contain semicolons.
        if (value.StartsWith("\"", StringComparison.Ordinal))
        {
            int end = value.IndexOf('"', 1);
            if (end > 0)
            {
                var rest = value.Substring(end + 1).TrimStart(';').Split(';');
                return new[] { value.Substring(1, end - 1) }.Concat(rest).ToArray();
            }
        }

        return value.Split(';');
    }

    // "Namespace.Type, Alias" -> "Namespace.Type, Full, Assembly, Name".
    private static string FullName(string type, Dictionary<string, string> aliases)
    {
        if (type == null)
        {
            return null;
        }

        int comma = type.IndexOf(',');
        if (comma < 0)
        {
            return type;
        }

        string assembly = type.Substring(comma + 1).Trim();
        return aliases.TryGetValue(assembly, out string full) ? type.Substring(0, comma) + ", " + full : type;
    }

    private static bool IsType(string type, string wanted)
    {
        return type != null && (type == wanted || type.StartsWith(wanted + ",", StringComparison.Ordinal));
    }

    private static bool TryPrimitive(string type, string value, out object result)
    {
        result = null;
        if (type == null || value == null)
        {
            return false;
        }

        CultureInfo invariant = CultureInfo.InvariantCulture;
        string simple = type.Split(',')[0].Trim();
        try
        {
            switch (simple)
            {
                case "System.Boolean": result = bool.Parse(value); return true;
                case "System.Byte": result = byte.Parse(value, invariant); return true;
                case "System.SByte": result = sbyte.Parse(value, invariant); return true;
                case "System.Char": result = char.Parse(value); return true;
                case "System.Int16": result = short.Parse(value, invariant); return true;
                case "System.UInt16": result = ushort.Parse(value, invariant); return true;
                case "System.Int32": result = int.Parse(value, invariant); return true;
                case "System.UInt32": result = uint.Parse(value, invariant); return true;
                case "System.Int64": result = long.Parse(value, invariant); return true;
                case "System.UInt64": result = ulong.Parse(value, invariant); return true;
                case "System.Single": result = float.Parse(value, invariant); return true;
                case "System.Double": result = double.Parse(value, invariant); return true;
                case "System.Decimal": result = decimal.Parse(value, invariant); return true;
                default: return false;
            }
        }
        catch (FormatException)
        {
            return false;       // stored as a string for the TypeConverter instead
        }
    }
}
