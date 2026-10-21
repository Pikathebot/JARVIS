namespace Jarvis.Core.Models;

/// <summary>Settings -> Remote (PLAN 4.19): <c>GET/POST /api/remote/pairing</c>.</summary>
public sealed class RemotePairing
{
    public string Code { get; set; } = "";
    public double ExpiresAt { get; set; }
    public int TtlSeconds { get; set; }

    /// <summary>What the phone opens (<c>https://&lt;pc&gt;.ts.net/m/#pair=CODE</c>); null when
    /// Tailscale isn't running here, so there is no address to give.</summary>
    public string? Url { get; set; }

    /// <summary>The QR of <see cref="Url"/> as an SVG document, black on white.</summary>
    public string? QrSvg { get; set; }

    public TailscaleState Tailscale { get; set; } = new();
}

public sealed class TailscaleState
{
    public bool Installed { get; set; }
    public bool Running { get; set; }
    public string? Host { get; set; }

    /// <summary><c>tailscale serve</c> forwards to the backend's remote port.</summary>
    public bool Serving { get; set; }
    public string? ServeCommand { get; set; }
}

/// <summary>GET /api/thread/head.</summary>
public sealed class ThreadHead
{
    public long LastMessageId { get; set; }
}

public sealed class RemoteDeviceList
{
    public List<RemoteDevice> Devices { get; set; } = new();
    public TailscaleState Tailscale { get; set; } = new();
}

public sealed class RemoteDevice
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public double CreatedAt { get; set; }
    public double LastUsedAt { get; set; }

    /// <summary>The device turned on notifications (PLAN 4.19 phase 2).</summary>
    public bool Notifications { get; set; }
}
