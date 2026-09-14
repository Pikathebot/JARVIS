using Microsoft.UI.Xaml;

namespace Jarvis_Glass;

/// <summary>
/// Where the controls of one window publish their glass geometry and text for that window's
/// <see cref="GlassHost"/> to render each frame. Each publisher owns a keyed slot (so a control
/// re-publishing on every animation tick replaces its own shapes rather than accumulating); the
/// renderer takes a snapshot per frame. One scene per hosted window -- a control finds its own
/// through <see cref="Find"/> (keyed by the window the element lives in), so the same control
/// class works in the main window and the HUD without knowing which it is in. Lock-guarded
/// because a publisher may run on a different thread from the render tick.
/// </summary>
public sealed class GlassScene
{
    private static readonly object RegistryGate = new();
    private static readonly Dictionary<ulong, GlassScene> ByWindowId = new();

    private readonly object _gate = new();
    private readonly Dictionary<object, GlassShape[]> _shapes = new();
    private readonly Dictionary<object, GlassText[]> _texts = new();
    private GlassShape[] _shapeSnapshot = Array.Empty<GlassShape>();
    private GlassText[] _textSnapshot = Array.Empty<GlassText>();
    private bool _shapesDirty, _textsDirty;

    public const int MaxShapes = 16;

    /// <summary>The scene of the window <paramref name="element"/> is in, or null while the
    /// element is not yet in a hosted window's tree (controls should simply skip publishing).</summary>
    public static GlassScene? Find(UIElement element)
    {
        var root = element.XamlRoot;
        if (root is null) return null;
        ulong id;
        try { id = root.ContentIslandEnvironment.AppWindowId.Value; }
        catch { return null; }
        lock (RegistryGate) return ByWindowId.TryGetValue(id, out var scene) ? scene : null;
    }

    internal static void Register(ulong windowId, GlassScene scene)
    {
        lock (RegistryGate) ByWindowId[windowId] = scene;
    }

    internal static void Unregister(ulong windowId)
    {
        lock (RegistryGate) ByWindowId.Remove(windowId);
    }

    public void Publish(object owner, params GlassShape[] shapes)
    {
        lock (_gate) { _shapes[owner] = shapes; _shapesDirty = true; }
    }

    public void PublishText(object owner, params GlassText[] texts)
    {
        lock (_gate) { _texts[owner] = texts; _textsDirty = true; }
    }

    /// <summary>Drops both the owner's shapes and its text.</summary>
    public void Remove(object owner)
    {
        lock (_gate)
        {
            if (_shapes.Remove(owner)) _shapesDirty = true;
            if (_texts.Remove(owner)) _textsDirty = true;
        }
    }

    /// <summary>Shapes ordered by layer, truncated to <see cref="MaxShapes"/>.</summary>
    public GlassShape[] SnapshotShapes()
    {
        lock (_gate)
        {
            if (_shapesDirty)
            {
                _shapeSnapshot = _shapes.Values.SelectMany(s => s).OrderBy(s => s.Params2.Y).Take(MaxShapes).ToArray();
                _shapesDirty = false;
            }
            return _shapeSnapshot;
        }
    }

    public GlassText[] SnapshotTexts()
    {
        lock (_gate)
        {
            if (_textsDirty)
            {
                _textSnapshot = _texts.Values.SelectMany(t => t).ToArray();
                _textsDirty = false;
            }
            return _textSnapshot;
        }
    }
}
