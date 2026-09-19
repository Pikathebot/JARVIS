using System.Diagnostics;
using System.Runtime.InteropServices;
using Jarvis.Core.Api;

namespace Jarvis_App.Services;

/// <summary>
/// Supervises the FastAPI backend, the role the retired run_jarvis.py launcher used to play.
/// Probes /health; if the backend isn't already running (e.g. a dev server started by hand),
/// spawns ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000" with
/// cwd=backend and PYTHONPATH=backend;repoRoot. The child is bound to a Win32 Job Object with
/// KILL_ON_JOB_CLOSE so it cannot outlive this process even if Jarvis.App crashes — the Python
/// launcher had no equivalent guarantee.
/// </summary>
public sealed class BackendHost : IDisposable
{
    private readonly JarvisApiClient _api;
    private readonly string _repoRoot;
    private Process? _process;
    private nint _jobHandle;

    public BackendHost(JarvisApiClient api, string repoRoot)
    {
        _api = api;
        _repoRoot = repoRoot;
    }

    /// <summary>Returns once /health responds, starting the backend first if needed.</summary>
    public async Task<bool> EnsureRunningAsync(TimeSpan timeout, CancellationToken ct = default)
    {
        if (await IsHealthyAsync(ct).ConfigureAwait(false))
        {
            return true;
        }

        Start();

        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (await IsHealthyAsync(ct).ConfigureAwait(false))
            {
                return true;
            }
            await Task.Delay(300, ct).ConfigureAwait(false);
        }

        return false;
    }

    private async Task<bool> IsHealthyAsync(CancellationToken ct)
    {
        try
        {
            await _api.FetchHealthAsync(ct).ConfigureAwait(false);
            return true;
        }
        catch
        {
            return false;
        }
    }

    private void Start()
    {
        var pythonExe = Path.Combine(_repoRoot, ".venv", "Scripts", "python.exe");
        var backendDir = Path.Combine(_repoRoot, "backend");
        var logPath = Path.Combine(_repoRoot, "backend.log");

        var startInfo = new ProcessStartInfo
        {
            FileName = File.Exists(pythonExe) ? pythonExe : "python",
            WorkingDirectory = backendDir,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        startInfo.ArgumentList.Add("-m");
        startInfo.ArgumentList.Add("uvicorn");
        startInfo.ArgumentList.Add("app.main:app");
        startInfo.ArgumentList.Add("--host");
        startInfo.ArgumentList.Add("127.0.0.1");
        startInfo.ArgumentList.Add("--port");
        startInfo.ArgumentList.Add("8000");

        startInfo.Environment["PYTHONPATH"] = $"{backendDir};{_repoRoot}";

        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };

        var logStream = new StreamWriter(File.Open(logPath, FileMode.Append, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
        _process.OutputDataReceived += (_, e) => { if (e.Data is not null) logStream.WriteLine(e.Data); };
        _process.ErrorDataReceived += (_, e) => { if (e.Data is not null) logStream.WriteLine(e.Data); };

        _process.Start();
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();

        AttachToJobObject(_process);
    }

    private void AttachToJobObject(Process process)
    {
        _jobHandle = JobObjectInterop.CreateJobObject(nint.Zero, null);
        if (_jobHandle == nint.Zero)
        {
            return;
        }

        var info = new JobObjectInterop.JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            LimitFlags = JobObjectInterop.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        var extendedInfo = new JobObjectInterop.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            BasicLimitInformation = info,
        };

        var length = Marshal.SizeOf(typeof(JobObjectInterop.JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        var extendedInfoPtr = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(extendedInfo, extendedInfoPtr, false);
            JobObjectInterop.SetInformationJobObject(
                _jobHandle,
                JobObjectInterop.JobObjectExtendedLimitInformation,
                extendedInfoPtr,
                (uint)length);
        }
        finally
        {
            Marshal.FreeHGlobal(extendedInfoPtr);
        }

        JobObjectInterop.AssignProcessToJobObject(_jobHandle, process.Handle);
    }

    public void Dispose()
    {
        if (_jobHandle != nint.Zero)
        {
            // Closing the job handle triggers KILL_ON_JOB_CLOSE, terminating uvicorn (and any
            // llama-server.exe it spawned) even if this process is exiting abnormally.
            JobObjectInterop.CloseHandle(_jobHandle);
            _jobHandle = nint.Zero;
        }
        _process?.Dispose();
    }
}

internal static class JobObjectInterop
{
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
    public const int JobObjectExtendedLimitInformation = 9;

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public nuint MinimumWorkingSetSize;
        public nuint MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public nint Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public nuint ProcessMemoryLimit;
        public nuint JobMemoryLimit;
        public nuint PeakProcessMemoryUsed;
        public nuint PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern nint CreateJobObject(nint lpJobAttributes, string? lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(nint hJob, int infoType, nint lpJobObjectInfo, uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(nint hJob, nint hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(nint hObject);
}
