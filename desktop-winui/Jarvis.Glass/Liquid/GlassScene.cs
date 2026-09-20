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
    private int _version;

    /// <summary>Bumped on every publish/remove. The host renders only when this, the capture
    /// frame, or the window geometry changed since its last frame -- a static scene over a
    /// static desktop costs no GPU.</summary>
    public int Version => Volatile.Read(ref _version);

    /// <summary>Per-window budget. The main window alone is pane + 5 slabs + ~7 pills + 2 toggles + a
    /// segmented (2 shapes each), and every visible message bubble and session row is a slab
    /// too -- 48 truncated the top layer (thumbs) off with a long chat. Shapes are 112 bytes
    /// each, so 96 is a 10.8 KB cbuffer; the shaders loop only over the active layer's shapes,
    /// so the count that matters per pixel is a layer's, not the total.</summary>
    public const int MaxShapes = 96;

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

    /// <summary>A publish identical to the owner's current shapes is a no-op: controls publish
    /// on every LayoutUpdated, and a bumped version would re-render the window for nothing.</summary>
    public void Publish(object owner, params GlassShape[] shapes)
    {
        lock (_gate)
        {
            if (_shapes.TryGetValue(owner, out var old)
                && System.Runtime.InteropServices.MemoryMarshal.AsBytes(old.AsSpan()).SequenceEqual(System.Runtime.InteropServices.MemoryMarshal.AsBytes(shapes.AsSpan())))
            {
                return;
            }
            _shapes[owner] = shapes; _shapesDirty = true; _version++;
        }
    }

    public void PublishText(object owner, params GlassText[] texts)
    {
        lock (_gate)
        {
            if (_texts.TryGetValue(owner, out var old) && old.AsSpan().SequenceEqual(texts)) return;
            _texts[owner] = texts; _textsDirty = true; _version++;
        }
    }

    /// <summary>Drops both the owner's shapes and its text.</summary>
    public void Remove(object owner)
    {
        lock (_gate)
        {
            if (_shapes.Remove(owner)) { _shapesDirty = true; _version++; }
            if (_texts.Remove(owner)) { _textsDirty = true; _version++; }
        }
    }

    /// <summary>The host prepends its layer-0 pane to whatever this returns and the cbuffer holds
    /// <see cref="MaxShapes"/> in total, so the scene itself gets one less -- otherwise the host's
    /// own truncation cut the last (highest) shape.</summary>
    public const int SceneBudget = MaxShapes - 1;

    /// <summary>Shapes ordered by layer, at most <see cref="SceneBudget"/> of them.</summary>
    public GlassShape[] SnapshotShapes()
    {
        lock (_gate)
        {
            if (_shapesDirty)
            {
                _shapeSnapshot = FitToBudget(_shapes.Values.SelectMany(s => s).ToList());
                _shapesDirty = false;
            }
            return _shapeSnapshot;
        }
    }

    private bool _overBudgetLogged;

    /// <summary>Over budget, shed from the most crowded layer -- a long list of nested rows --
    /// never by cutting the top layers off. Sorting by layer and truncating dropped whatever was
    /// highest, i.e. the toggle thumbs and any open dropdown, the moment a list got long: the
    /// controls silently vanished while the rows that caused it stayed. Losing a few rows at the
    /// end of a list is visible but harmless; losing the controls is not.</summary>
    private GlassShape[] FitToBudget(List<GlassShape> all)
    {
        if (all.Count > SceneBudget)
        {
            if (!_overBudgetLogged)
            {
                _overBudgetLogged = true;
                GlassLog.Write($"scene has {all.Count} shapes, budget is {SceneBudget}; shedding from the most crowded layer");
            }
            // Owners are kept in publish order, so the last shapes of a layer are the newest /
            // furthest down a list; drop from that end.
            var byLayer = all.Select((shape, index) => (shape, index))
                .GroupBy(t => (int)t.shape.Params2.Y)
                .ToDictionary(g => g.Key, g => g.Select(t => t.index).ToList());
            var drop = new HashSet<int>();
            while (all.Count - drop.Count > SceneBudget)
            {
                var crowded = byLayer.Values.OrderByDescending(v => v.Count).First();
                drop.Add(crowded[^1]);
                crowded.RemoveAt(crowded.Count - 1);
            }
            all = all.Where((_, index) => !drop.Contains(index)).ToList();
        }
        // Stable, so a layer keeps its publish order.
        return all.OrderBy(s => s.Params2.Y).ToArray();
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
