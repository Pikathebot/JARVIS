namespace Jarvis_Glass;

/// <summary>
/// The window's glass layer census, in one place.
///
/// A layer's source is the *finished output of the layer below it*: frost blurs it, refraction
/// bends it, tint sits over it. Two things follow, and both bit us when the sheets were declared
/// `Layer="2"` by hand in XAML:
///
///   * a shape only obscures what is on a LOWER layer -- same-layer shapes are siblings drawn in
///     an arbitrary order (<see cref="GlassScene"/> sorts by layer alone, so ties fall out in
///     dictionary order), so a sheet sharing a layer with the controls behind it could be painted
///     over by them;
///   * a shape only blurs what is on a lower layer, so that sheet never frosted those controls
///     however high its Frost went.
///
/// Layers are not free -- each one is a full-screen displacement + refraction pass, plus a
/// Gaussian pre-pass when anything on it is frosted (cached while nothing on the layer changed,
/// see GlassRenderer's LayerCache) -- so this is a short ladder, not a z-index.
///
/// <code>
///   0  the live desktop capture (the renderer's own base; nothing publishes here)
///   1  panels: every GlassSlab that doesn't say otherwise -- sidebar, chat, composer, bubbles
///   2  the controls inside those panels: GlassButton, GlassSegmented, GlassTextField, GlassToggle
///      (GlassSlab.BaseLayerFor = nearest ancestor slab's Layer + 1)
///   3  those controls' moving parts: a lifted toggle thumb, a slider thumb, a segmented puck
///   3  the workspace dropdown, which floats over the panels  (4-5 for its own rows and puck)
///   4  a modal sheet -- above every one of the above, so it covers AND frosts all of it
///   5  the sheet's own controls   (6 for their moving parts)
/// </code>
/// </summary>
public static class GlassLayers
{
    /// <summary>A top-level panel. Also <see cref="GlassSlab"/>'s default, so panels say nothing.</summary>
    public const int Panel = 1;

    /// <summary>A popover over the panels: the workspace dropdown. Its rows and puck take 4 and 5.</summary>
    public const int Popover = 3;

    /// <summary>A modal sheet (Settings, New workspace). Must stay above every layer a panel's
    /// controls can reach -- <see cref="Popover"/> plus one for its moving parts -- or the window
    /// behind it reads straight through, which is the bug this constant exists to prevent.</summary>
    public const int Sheet = 4;
}
