namespace Jarvis_Glass;

/// <summary>
/// The design system's glass variants (tokens: material family). A <see cref="GlassSlab"/>'s
/// material picks its tint colour and its tint floor per appearance; frost, bezel and shadow
/// stay whatever the slab sets.
/// </summary>
public enum GlassMaterial
{
    /// <summary>Panels and assistant bubbles: glass-tint, floored at the appearance's
    /// regular-tint floor (0.20 dark, 0.80 light, 0.75 high contrast).</summary>
    Regular,

    /// <summary>Sheets, the popover, the HUD: glass-tint, floored at the thick-tint floor
    /// (0.62 dark, 0.90 light and high contrast).</summary>
    Thick,

    /// <summary>Glass resting on a panel (session rows): its own tint amount, no floor.</summary>
    Clear,

    /// <summary>The user's bubble and other accent surfaces: tinted toward accent-fill at
    /// glass-accent-tint, so on-accent text reads on it.</summary>
    Accent,
}
