using Microsoft.Windows.AppLifecycle;

namespace Jarvis_App.Services;

/// <summary>
/// Replaces the port-57321 TCP lock in the retired run_jarvis.py's acquire_single_instance_lock() with the
/// Windows App SDK's AppInstance key registration. A second launch redirects activation to the
/// already-running instance instead of starting a second copy.
/// </summary>
public static class SingleInstanceService
{
    private const string InstanceKey = "Jarvis.SingleInstance";

    /// <summary>Returns true if this process should continue starting up (it is the main instance
    /// or the redirect could not be completed); false if activation was redirected and this
    /// process should exit immediately.</summary>
    public static bool ClaimOrRedirect()
    {
        var instance = AppInstance.FindOrRegisterForKey(InstanceKey);
        if (instance.IsCurrent)
        {
            return true;
        }

        var args = AppInstance.GetCurrent().GetActivatedEventArgs();
        instance.RedirectActivationToAsync(args).AsTask().GetAwaiter().GetResult();
        return false;
    }
}
