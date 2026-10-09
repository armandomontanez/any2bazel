// Copyright 2026 EngFlow Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// An MSBuild logger that writes the build's events as NDJSON (one JSON object
// per line). It is the capture half of the MSBuild frontend and is kept
// deliberately dumb: every event is written with its raw fields and nothing is
// interpreted here. All correlation and interpretation happens in Python
// (scripts/msbuild_events.py, scripts/extract_msbuild.py), where it is tested.
//
// Normal use is REPLAY: MSBuild reads its own binary log and feeds the recorded
// events through this logger, so the binlog reader always matches the MSBuild
// that is installed:
//
//   msbuild build.binlog -noconlog -logger:Any2BazelLogger,<dll>;out=<events.ndjson>;filter=<filter.txt>
//
// It is compiled by scripts/msbuild_events.py with the csc.exe that ships
// inside MSBuild, against the netstandard reference assemblies MSBuild also
// ships (the same recipe RoslynCodeTaskFactory uses for inline tasks), so no
// .NET SDK and no NuGet package are needed.
//
// Parameters (';'-separated key=value):
//   out=<path>      NDJSON output file (required)
//   filter=<path>   optional allowlist file, one entry per line:
//                     P <name>      evaluated property to dump ('Foo*' = prefix, '*' = all)
//                     I <itemtype>  evaluated item type to dump ('*' = all)
//                     M <taskname>  task whose plain messages are dumped ('*' = all)
//                     X <taskname>  task whose parameters are NOT dumped
//                   Without a filter file everything is dumped.
//
// The one piece of bookkeeping done here is size control: a property value
// longer than LongValueThreshold is written in full once, as its own
// {"e":"LongValue","id":N,"value":...} record, and referenced afterwards as the
// string "@long:N". (A solution build repeats a ~50 KB configuration XML in the
// global properties of every project instance.)
//
// Record shapes are documented where they are consumed, in
// scripts/msbuild_events.py. Context keys shared by most records:
//   p = ProjectContextId, t = TargetId, k = TaskId, ev = EvaluationId, n = NodeId

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using Microsoft.Build.Framework;

public sealed class Any2BazelLogger : ILogger
{
    private const int SchemaVersion = 1;

    public LoggerVerbosity Verbosity { get; set; }
    public string Parameters { get; set; }

    private readonly object _lock = new object();
    private StreamWriter _out;

    private bool _allProps = true;
    private bool _allItems = true;
    private bool _allMessages = false;
    private readonly HashSet<string> _props = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private readonly List<string> _propPrefixes = new List<string>();
    private readonly HashSet<string> _items = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _messageTasks = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _quietTasks = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    private const int LongValueThreshold = 2048;
    private readonly Dictionary<string, int> _longValues = new Dictionary<string, int>();
    private readonly List<string> _pending = new List<string>();

    // (project, target, task) -> task name, for attributing plain messages.
    private readonly Dictionary<string, string> _runningTasks = new Dictionary<string, string>();

    // TaskParameterEventArgs.ParameterName only exists in newer MSBuild.
    private static readonly PropertyInfo ParameterNameProperty =
        typeof(TaskParameterEventArgs).GetProperty("ParameterName");

    public void Initialize(IEventSource eventSource)
    {
        string outPath = null;
        string filterPath = null;
        foreach (string part in (Parameters ?? "").Split(';'))
        {
            int eq = part.IndexOf('=');
            if (eq <= 0) continue;
            string key = part.Substring(0, eq).Trim();
            string value = part.Substring(eq + 1).Trim().Trim('"');
            if (key.Equals("out", StringComparison.OrdinalIgnoreCase)) outPath = value;
            else if (key.Equals("filter", StringComparison.OrdinalIgnoreCase)) filterPath = value;
        }
        if (string.IsNullOrEmpty(outPath))
            throw new LoggerException("Any2BazelLogger: the 'out=<path>' parameter is required");
        if (!string.IsNullOrEmpty(filterPath)) LoadFilter(filterPath);

        _out = new StreamWriter(outPath, false, new UTF8Encoding(false));
        _out.NewLine = "\n";
        _out.WriteLine("{\"e\":\"Dump\",\"schema\":" + SchemaVersion + "}");

        // Only matter when attached to a live build; a replay already carries
        // whatever the binlog recorded.
        IEventSource3 source3 = eventSource as IEventSource3;
        if (source3 != null) source3.IncludeTaskInputs();
        IEventSource4 source4 = eventSource as IEventSource4;
        if (source4 != null) source4.IncludeEvaluationPropertiesAndItems();

        eventSource.AnyEventRaised += OnEvent;
    }

    public void Shutdown()
    {
        lock (_lock)
        {
            if (_out != null)
            {
                _out.Flush();
                _out.Dispose();
                _out = null;
            }
        }
    }

    private void LoadFilter(string path)
    {
        _allProps = false;
        _allItems = false;
        foreach (string raw in File.ReadAllLines(path))
        {
            string line = raw.Trim();
            if (line.Length < 3 || line[1] != ' ') continue;
            string value = line.Substring(2).Trim();
            switch (line[0])
            {
                case 'P':
                    if (value == "*") _allProps = true;
                    else if (value.EndsWith("*")) _propPrefixes.Add(value.Substring(0, value.Length - 1));
                    else _props.Add(value);
                    break;
                case 'I':
                    if (value == "*") _allItems = true;
                    else _items.Add(value);
                    break;
                case 'M':
                    if (value == "*") _allMessages = true;
                    else _messageTasks.Add(value);
                    break;
                case 'X':
                    _quietTasks.Add(value);
                    break;
            }
        }
    }

    private bool WantProperty(string name)
    {
        if (_allProps || _props.Contains(name)) return true;
        foreach (string prefix in _propPrefixes)
            if (name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    private bool WantItem(string itemType)
    {
        return _allItems || _items.Contains(itemType);
    }

    private void OnEvent(object sender, BuildEventArgs e)
    {
        lock (_lock)
        {
            if (_out == null) return;
            StringBuilder sb = new StringBuilder(256);
            bool wanted;
            _pending.Clear();
            try
            {
                wanted = Format(sb, e);
            }
            catch (Exception ex)
            {
                wanted = true;
                sb.Length = 0;
                sb.Append("{\"e\":\"DumperError\",\"type\":");
                Str(sb, e.GetType().Name);
                sb.Append(",\"msg\":");
                Str(sb, ex.GetType().Name + ": " + ex.Message);
                sb.Append('}');
            }
            if (!wanted) return;
            foreach (string record in _pending) _out.WriteLine(record);
            _out.WriteLine(sb.ToString());
        }
    }

    // Returns false when the event is not dumped. Derived event types are
    // tested before their bases (several of them derive from
    // BuildMessageEventArgs).
    private bool Format(StringBuilder sb, BuildEventArgs e)
    {
        ProjectEvaluationFinishedEventArgs evalFinished = e as ProjectEvaluationFinishedEventArgs;
        if (evalFinished != null)
        {
            Open(sb, "Eval", e);
            Field(sb, "file", evalFinished.ProjectFile);
            sb.Append(",\"global\":"); Properties(sb, evalFinished.GlobalProperties, false);
            sb.Append(",\"props\":"); Properties(sb, evalFinished.Properties, true);
            sb.Append(",\"items\":"); EvaluatedItems(sb, evalFinished.Items);
            sb.Append('}');
            return true;
        }

        ProjectStartedEventArgs projectStarted = e as ProjectStartedEventArgs;
        if (projectStarted != null)
        {
            Open(sb, "ProjectStarted", e);
            Field(sb, "file", projectStarted.ProjectFile);
            Field(sb, "targets", projectStarted.TargetNames);
            BuildEventContext parent = projectStarted.ParentProjectBuildEventContext;
            if (parent != null)
            {
                sb.Append(",\"parent\":").Append(parent.ProjectContextId);
                sb.Append(",\"parentTarget\":").Append(parent.TargetId);
                sb.Append(",\"parentTask\":").Append(parent.TaskId);
            }
            sb.Append(",\"global\":"); Properties(sb, projectStarted.GlobalProperties, false);
            // Older MSBuild logs properties and items here instead of on the
            // evaluation; newer MSBuild leaves these null.
            if (projectStarted.Properties != null)
            {
                sb.Append(",\"props\":"); Properties(sb, projectStarted.Properties, true);
            }
            if (projectStarted.Items != null)
            {
                sb.Append(",\"items\":"); EvaluatedItems(sb, projectStarted.Items);
            }
            sb.Append('}');
            return true;
        }

        ProjectFinishedEventArgs projectFinished = e as ProjectFinishedEventArgs;
        if (projectFinished != null)
        {
            Open(sb, "ProjectFinished", e);
            Field(sb, "file", projectFinished.ProjectFile);
            Bool(sb, "succeeded", projectFinished.Succeeded);
            sb.Append('}');
            return true;
        }

        TargetStartedEventArgs targetStarted = e as TargetStartedEventArgs;
        if (targetStarted != null)
        {
            Open(sb, "TargetStarted", e);
            Field(sb, "name", targetStarted.TargetName);
            Field(sb, "file", targetStarted.TargetFile);
            Field(sb, "parent", targetStarted.ParentTarget);
            sb.Append('}');
            return true;
        }

        TargetFinishedEventArgs targetFinished = e as TargetFinishedEventArgs;
        if (targetFinished != null)
        {
            Open(sb, "TargetFinished", e);
            Field(sb, "name", targetFinished.TargetName);
            Bool(sb, "succeeded", targetFinished.Succeeded);
            sb.Append('}');
            return true;
        }

        TargetSkippedEventArgs targetSkipped = e as TargetSkippedEventArgs;
        if (targetSkipped != null)
        {
            Open(sb, "TargetSkipped", e);
            Field(sb, "name", targetSkipped.TargetName);
            Field(sb, "reason", targetSkipped.SkipReason.ToString());
            Field(sb, "file", targetSkipped.TargetFile);
            Field(sb, "parent", targetSkipped.ParentTarget);
            Field(sb, "condition", targetSkipped.Condition);
            Field(sb, "evaluated", targetSkipped.EvaluatedCondition);
            if (targetSkipped.OriginalBuildEventContext != null)
                sb.Append(",\"originalProject\":").Append(targetSkipped.OriginalBuildEventContext.ProjectContextId);
            sb.Append('}');
            return true;
        }

        TaskStartedEventArgs taskStarted = e as TaskStartedEventArgs;
        if (taskStarted != null)
        {
            _runningTasks[TaskKey(e.BuildEventContext)] = taskStarted.TaskName;
            Open(sb, "TaskStarted", e);
            Field(sb, "name", taskStarted.TaskName);
            Field(sb, "file", taskStarted.TaskFile);
            sb.Append(",\"line\":").Append(taskStarted.LineNumber);
            sb.Append('}');
            return true;
        }

        TaskFinishedEventArgs taskFinished = e as TaskFinishedEventArgs;
        if (taskFinished != null)
        {
            _runningTasks.Remove(TaskKey(e.BuildEventContext));
            Open(sb, "TaskFinished", e);
            Field(sb, "name", taskFinished.TaskName);
            Bool(sb, "succeeded", taskFinished.Succeeded);
            sb.Append('}');
            return true;
        }

        TaskCommandLineEventArgs commandLine = e as TaskCommandLineEventArgs;
        if (commandLine != null)
        {
            Open(sb, "CommandLine", e);
            Field(sb, "task", commandLine.TaskName);
            Field(sb, "cmd", commandLine.CommandLine);
            sb.Append('}');
            return true;
        }

        TaskParameterEventArgs taskParameter = e as TaskParameterEventArgs;
        if (taskParameter != null)
        {
            TaskParameterMessageKind kind = taskParameter.Kind;
            bool isItemMutation = kind == TaskParameterMessageKind.AddItem
                || kind == TaskParameterMessageKind.RemoveItem;
            if (isItemMutation && !WantItem(taskParameter.ItemType ?? "")) return false;
            if (_quietTasks.Count > 0 && e.BuildEventContext != null && e.BuildEventContext.TaskId >= 0)
            {
                string owner;
                if (_runningTasks.TryGetValue(TaskKey(e.BuildEventContext), out owner)
                    && _quietTasks.Contains(owner)) return false;
            }
            Open(sb, "TaskParameter", e);
            Field(sb, "kind", kind.ToString());
            string parameterName = null;
            if (ParameterNameProperty != null)
                parameterName = ParameterNameProperty.GetValue(taskParameter, null) as string;
            Field(sb, "name", parameterName ?? taskParameter.ItemType);
            Field(sb, "itemType", taskParameter.ItemType);
            sb.Append(",\"items\":");
            ItemList(sb, taskParameter.Items);
            sb.Append('}');
            return true;
        }

        ProjectImportedEventArgs imported = e as ProjectImportedEventArgs;
        if (imported != null)
        {
            if (string.IsNullOrEmpty(imported.ImportedProjectFile) || imported.ImportIgnored) return false;
            Open(sb, "Import", e);
            Field(sb, "file", imported.ProjectFile);
            Field(sb, "imported", imported.ImportedProjectFile);
            sb.Append('}');
            return true;
        }

        EnvironmentVariableReadEventArgs envRead = e as EnvironmentVariableReadEventArgs;
        if (envRead != null)
        {
            // Name only: values can be secrets, and the ones that matter are
            // visible as evaluated properties.
            Open(sb, "EnvRead", e);
            Field(sb, "name", envRead.EnvironmentVariableName);
            sb.Append('}');
            return true;
        }

        BuildErrorEventArgs error = e as BuildErrorEventArgs;
        if (error != null)
        {
            Open(sb, "Error", e);
            Field(sb, "code", error.Code);
            Field(sb, "file", error.File);
            sb.Append(",\"line\":").Append(error.LineNumber);
            Field(sb, "project", error.ProjectFile);
            Field(sb, "msg", error.Message);
            sb.Append('}');
            return true;
        }

        BuildFinishedEventArgs buildFinished = e as BuildFinishedEventArgs;
        if (buildFinished != null)
        {
            Open(sb, "BuildFinished", e);
            Bool(sb, "succeeded", buildFinished.Succeeded);
            sb.Append('}');
            return true;
        }

        if (e is BuildStartedEventArgs)
        {
            Open(sb, "BuildStarted", e);
            sb.Append('}');
            return true;
        }

        // Plain messages, only for allowlisted tasks. Scalar task parameters
        // ("Task Parameter:Name=Value") are logged this way by MSBuild.
        if (e.GetType() == typeof(BuildMessageEventArgs) && e.BuildEventContext != null
            && e.BuildEventContext.TaskId >= 0)
        {
            string taskName;
            _runningTasks.TryGetValue(TaskKey(e.BuildEventContext), out taskName);
            if (taskName == null) return false;
            if (!_allMessages && !_messageTasks.Contains(taskName)) return false;
            Open(sb, "Message", e);
            Field(sb, "task", taskName);
            Field(sb, "msg", e.Message);
            sb.Append('}');
            return true;
        }

        return false;
    }

    private static string TaskKey(BuildEventContext c)
    {
        if (c == null) return "";
        return c.ProjectContextId + "/" + c.TargetId + "/" + c.TaskId;
    }

    // ---- JSON writing -------------------------------------------------------

    private static void Open(StringBuilder sb, string kind, BuildEventArgs e)
    {
        sb.Append("{\"e\":\"").Append(kind).Append('"');
        BuildEventContext c = e.BuildEventContext;
        if (c == null) return;
        if (c.ProjectContextId != BuildEventContext.InvalidProjectContextId)
            sb.Append(",\"p\":").Append(c.ProjectContextId);
        if (c.TargetId != BuildEventContext.InvalidTargetId)
            sb.Append(",\"t\":").Append(c.TargetId);
        if (c.TaskId != BuildEventContext.InvalidTaskId)
            sb.Append(",\"k\":").Append(c.TaskId);
        if (c.EvaluationId != BuildEventContext.InvalidEvaluationId)
            sb.Append(",\"ev\":").Append(c.EvaluationId);
        if (c.NodeId != BuildEventContext.InvalidNodeId)
            sb.Append(",\"n\":").Append(c.NodeId);
    }

    private static void Field(StringBuilder sb, string name, string value)
    {
        if (value == null) return;
        sb.Append(",\"").Append(name).Append("\":");
        Str(sb, value);
    }

    private static void Bool(StringBuilder sb, string name, bool value)
    {
        sb.Append(",\"").Append(name).Append("\":").Append(value ? "true" : "false");
    }

    private static void Str(StringBuilder sb, string s)
    {
        if (s == null)
        {
            sb.Append("null");
            return;
        }
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20 || (c >= 0xD800 && c <= 0xDFFF) || c == 0x2028 || c == 0x2029)
                        sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else
                        sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }

    // Accepts the shapes MSBuild uses for property bags: DictionaryEntry or
    // KeyValuePair<string, string> sequences (IDictionary<string, string>
    // included). `filtered` applies the property allowlist.
    private void Properties(StringBuilder sb, IEnumerable properties, bool filtered)
    {
        sb.Append('{');
        bool first = true;
        if (properties != null)
        {
            foreach (object entry in properties)
            {
                string name;
                string value;
                if (entry is DictionaryEntry)
                {
                    DictionaryEntry de = (DictionaryEntry)entry;
                    name = de.Key as string;
                    value = de.Value as string ?? Convert.ToString(de.Value);
                }
                else if (entry is KeyValuePair<string, string>)
                {
                    KeyValuePair<string, string> pair = (KeyValuePair<string, string>)entry;
                    name = pair.Key;
                    value = pair.Value;
                }
                else
                {
                    continue;
                }
                if (name == null || (filtered && !WantProperty(name))) continue;
                if (!first) sb.Append(',');
                first = false;
                Str(sb, name);
                sb.Append(':');
                Str(sb, Shorten(value ?? ""));
            }
        }
        sb.Append('}');
    }

    private string Shorten(string value)
    {
        if (value.Length <= LongValueThreshold) return value;
        int id;
        if (!_longValues.TryGetValue(value, out id))
        {
            id = _longValues.Count;
            _longValues[value] = id;
            StringBuilder record = new StringBuilder(value.Length + 64);
            record.Append("{\"e\":\"LongValue\",\"id\":").Append(id).Append(",\"value\":");
            Str(record, value);
            record.Append('}');
            _pending.Add(record.ToString());
        }
        return "@long:" + id;
    }

    private void EvaluatedItems(StringBuilder sb, IEnumerable items)
    {
        sb.Append('[');
        bool first = true;
        if (items != null)
        {
            foreach (object entry in items)
            {
                if (!(entry is DictionaryEntry)) continue;
                DictionaryEntry de = (DictionaryEntry)entry;
                string itemType = de.Key as string;
                ITaskItem item = de.Value as ITaskItem;
                if (itemType == null || item == null || !WantItem(itemType)) continue;
                if (!first) sb.Append(',');
                first = false;
                sb.Append("{\"t\":");
                Str(sb, itemType);
                sb.Append(",\"i\":");
                Str(sb, item.ItemSpec);
                sb.Append(",\"m\":");
                Metadata(sb, item);
                sb.Append('}');
            }
        }
        sb.Append(']');
    }

    private static void ItemList(StringBuilder sb, IList items)
    {
        sb.Append('[');
        bool first = true;
        if (items != null)
        {
            foreach (object entry in items)
            {
                if (!first) sb.Append(',');
                first = false;
                ITaskItem item = entry as ITaskItem;
                sb.Append("{\"i\":");
                Str(sb, item != null ? item.ItemSpec : Convert.ToString(entry));
                if (item != null)
                {
                    sb.Append(",\"m\":");
                    Metadata(sb, item);
                }
                sb.Append('}');
            }
        }
        sb.Append(']');
    }

    private static void Metadata(StringBuilder sb, ITaskItem item)
    {
        sb.Append('{');
        bool first = true;
        IDictionary metadata = null;
        try { metadata = item.CloneCustomMetadata(); }
        catch (Exception) { }
        if (metadata != null)
        {
            foreach (DictionaryEntry pair in metadata)
            {
                if (!first) sb.Append(',');
                first = false;
                Str(sb, Convert.ToString(pair.Key));
                sb.Append(':');
                Str(sb, Convert.ToString(pair.Value) ?? "");
            }
        }
        sb.Append('}');
    }
}
