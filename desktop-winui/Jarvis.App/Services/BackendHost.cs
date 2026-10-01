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
    private TextWriter? _log;
    private nint _jobHandle;

    public BackendHost(JarvisApiClient api, string repoRoot)
    {
        _api = api;
        _repoRoot = repoRoot;
    }

    private const int Port = 8000;

    /// <summary>Returns once /health responds, starting the backend first if needed.
    /// Nothing here connects to the port until something is listening on it: on Windows a
    /// connection to a closed localhost port takes ~2 s to be refused (it retries the SYN), so
    /// probing /health cost 2 s before uvicorn was even launched and then noticed it ready only
    /// in 2 s steps. The TCP listener table answers instantly.</summary>
    public async Task<bool> EnsureRunningAsync(TimeSpan timeout, CancellationToken ct = default)
    {
        if (IsListening() && await IsHealthyAsync(ct).ConfigureAwait(false))
        {
            return true; // already running (a dev server started by hand)
        }

        Start();

        var deadline = DateTime.UtcNow + timeout;
        var starts = 1;
        while (DateTime.UtcNow < deadline)
        {
            if (IsListening() && await IsHealthyAsync(ct).ConfigureAwait(false))
            {
                return true;
            }
            if (await RestartIfExitedAsync(starts, ct).ConfigureAwait(false)) starts++;
            else await Task.Delay(100, ct).ConfigureAwait(false);
        }

        // Past the caller's patience, not ours: the window opens offline, and this keeps
        // restarting uvicorn until it answers (the window refreshes when it does).
        _ = KeepStartingAsync(starts, ct);
        return false;
    }

    /// <summary>
    /// uvicorn sometimes dies within a second of launch: this machine intermittently refuses
    /// new sockets with WSAEACCES (10013) -- seen three times on 2026-09-24, from curl and every
    /// other process too, once for longer than 16 s -- and asyncio's event loop needs a loopback
    /// socketpair before it serves anything. Starting it again later works; the wait grows.
    /// </summary>
    private async Task<bool> RestartIfExitedAsync(int starts, CancellationToken ct)
    {
        if (_process is not { HasExited: true }) return false;
        var delay = TimeSpan.FromSeconds(Math.Min(MaxRestartDelay.TotalSeconds, RestartDelay.TotalSeconds * starts));
        App.Log($"backend: uvicorn exited with {_process.ExitCode} during startup; starting it again in {delay.TotalSeconds:F1} s (start {starts + 1})");
        await Task.Delay(delay, ct).ConfigureAwait(false);
        if (_disposed) return false; // the app is closing: a new child would outlive its job
        Start();
        return true;
    }

    private async Task KeepStartingAsync(int starts, CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested && !_disposed)
            {
                if (IsListening() && await IsHealthyAsync(ct).ConfigureAwait(false))
                {
                    App.Log($"backend: up after {starts} starts");
                    return;
                }
                if (await RestartIfExitedAsync(starts, ct).ConfigureAwait(false)) starts++;
                else await Task.Delay(250, ct).ConfigureAwait(false);
            }
        }
        catch (Exception ex)
        {
            App.Log($"backend: supervision stopped: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private static readonly TimeSpan RestartDelay = TimeSpan.FromSeconds(1.5);
    private static readonly TimeSpan MaxRestartDelay = TimeSpan.FromSeconds(10);
    private volatile bool _disposed;

    private static bool IsListening()
    {
        try
        {
            return System.Net.NetworkInformation.IPGlobalProperties.GetIPGlobalProperties()
                .GetActiveTcpListeners().Any(e => e.Port == Port);
        }
        catch
        {
            return true; // can't tell: fall back to probing
        }
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
        startInfo.ArgumentList.Add(Port.ToString());

        startInfo.Environment["PYTHONPATH"] = $"{backendDir};{_repoRoot}";
        // Python block-buffers stdout when it is a pipe; unbuffered so prints land in
        // backend.log as they happen rather than in 8 KB bursts.
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        // The backend's Windows toasts (reminders) go out under this app's identity, so they
        // show as "Jarvis" and a click opens the app. Unpackaged runs have no identity: skip.
        try
        {
            startInfo.Environment["JARVIS_AUMID"] = $"{Windows.ApplicationModel.Package.Current.Id.FamilyName}!App";
        }
        catch (InvalidOperationException) { }

        _process?.Dispose();
        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };

        // One writer for every start: the file is opened without write sharing, so a restart
        // can't open it a second time.
        var logStream = _log ??= TextWriter.Synchronized(new StreamWriter(File.Open(logPath, FileMode.Append, FileAccess.Write, FileShare.Read)) { AutoFlush = true });
        _process.OutputDataReceived += (_, e) => { if (e.Data is not null) logStream.WriteLine(e.Data); };
        _process.ErrorDataReceived += (_, e) => { if (e.Data is not null) logStream.WriteLine(e.Data); };

        _process.Start();
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();

        AttachToJobObject(_process);
    }

    private void AttachToJobObject(Process process)
    {
        // A restart gets a fresh job; closing the old one kills nothing (its process is gone).
        if (_jobHandle != nint.Zero) JobObjectInterop.CloseHandle(_jobHandle);
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
        _disposed = true;
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
