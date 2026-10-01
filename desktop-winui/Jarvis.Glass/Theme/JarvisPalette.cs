using System.Numerics;
using Windows.UI;

namespace Jarvis_Glass;

/// <summary>The three appearances of the Jarvis design system (version 2).</summary>
public enum JarvisAppearance
{
    Dark,
    Light,
    HighContrast,
}

/// <summary>
/// Every colour Jarvis paints, per appearance: the one source of truth for the design system's
/// colour tokens (the "Jarvis" Design System artifact, tokens.json v2). The app builds its XAML
/// theme brushes from this (JarvisTheme in Jarvis.App), the glass controls read it when they
/// publish, and Jarvis.GlassLab links this file in (it has no project reference on purpose).
///
/// Names match the tokens: <c>label-secondary</c> is <see cref="JarvisColors.LabelSecondary"/>
/// here and <c>LabelSecondaryBrush</c> in XAML. Pure data -- no XAML, no renderer -- so it stays
/// safe to share.
/// </summary>
public static class JarvisPalette
{
    public static readonly JarvisColors Dark = new()
    {
        Blue = Hex(0x0A84FF), Green = Hex(0x30D158), Orange = Hex(0xFF9F0A), Red = Hex(0xFF453A),
        Yellow = Hex(0xFFD60A), Purple = Hex(0xBF5AF2), Teal = Hex(0x40C8E0), Gray = Hex(0x8E8E93),

        Label = Hex(0xFFFFFF), LabelSecondary = Hex(0x98989F), LabelTertiary = Hex(0x6E6E73), Placeholder = Hex(0x86868B),
        GlassTint = Hex(0x1C1C1E), SurfaceCode = Hex(0x000000, 0xB3), SurfaceAvatar = Hex(0x3A3A3C),
        FillPrimary = Hex(0x787880, 0x5C), FillSecondary = Hex(0x787880, 0x52), FillTertiary = Hex(0x767680, 0x3D),
        Separator = Hex(0x545458, 0x99), Outline = Hex(0xFFFFFF, 0x26), Scrim = Hex(0x000000, 0x66),

        AccentText = Hex(0x0A84FF), AccentFill = Hex(0x0071E3),
        StatusSuccess = Hex(0x30D158), StatusWarning = Hex(0xFF9F0A), StatusError = Hex(0xFF453A),
        ToggleOff = Hex(0x787880, 0x52), WindowControlInactive = Hex(0x48484C),
        SyntaxString = Hex(0x30D158), SyntaxNumber = Hex(0xFF9F0A), SyntaxKeyword = Hex(0xBF5AF2), SyntaxType = Hex(0x40C8E0),

        RegularTintFloor = 0.20f, ThickTintFloor = 0.62f,
    };

    public static readonly JarvisColors Light = new()
    {
        Blue = Hex(0x007AFF), Green = Hex(0x34C759), Orange = Hex(0xFF9500), Red = Hex(0xFF3B30),
        Yellow = Hex(0xFFCC00), Purple = Hex(0xAF52DE), Teal = Hex(0x30B0C7), Gray = Hex(0x8E8E93),

        Label = Hex(0x000000), LabelSecondary = Hex(0x5B5B61), LabelTertiary = Hex(0x85858A), Placeholder = Hex(0x6C6C71),
        GlassTint = Hex(0xF2F2F7), SurfaceCode = Hex(0xFFFFFF, 0xCC), SurfaceAvatar = Hex(0xD1D1D6),
        FillPrimary = Hex(0x787880, 0x33), FillSecondary = Hex(0x787880, 0x29), FillTertiary = Hex(0x767680, 0x1F),
        Separator = Hex(0x3C3C43, 0x4A), Outline = Hex(0x000000, 0x1A), Scrim = Hex(0x000000, 0x33),

        AccentText = Hex(0x0062CC), AccentFill = Hex(0x0071E3),
        StatusSuccess = Hex(0x1E7B34), StatusWarning = Hex(0xC93400), StatusError = Hex(0xD70015),
        ToggleOff = Hex(0xE9E9EB), WindowControlInactive = Hex(0xD1D1D6),
        SyntaxString = Hex(0x1E7B34), SyntaxNumber = Hex(0xC93400), SyntaxKeyword = Hex(0x8944AB), SyntaxType = Hex(0x0B7285),

        RegularTintFloor = 0.80f, ThickTintFloor = 0.90f,
    };

    public static readonly JarvisColors HighContrast = new()
    {
        Blue = Hex(0x409CFF), Green = Hex(0x30DB5B), Orange = Hex(0xFFB340), Red = Hex(0xFF6961),
        Yellow = Hex(0xFFD426), Purple = Hex(0xDA8FFF), Teal = Hex(0x5DE6FF), Gray = Hex(0xAEAEB2),

        Label = Hex(0xFFFFFF), LabelSecondary = Hex(0xC7C7CC), LabelTertiary = Hex(0xAEAEB2), Placeholder = Hex(0xAEAEB2),
        GlassTint = Hex(0x000000), SurfaceCode = Hex(0x000000), SurfaceAvatar = Hex(0x3A3A3C),
        FillPrimary = Hex(0xFFFFFF, 0x3D), FillSecondary = Hex(0xFFFFFF, 0x29), FillTertiary = Hex(0xFFFFFF, 0x1F),
        Separator = Hex(0x8E8E93), Outline = Hex(0xAEAEB2), Scrim = Hex(0x000000, 0xB3),

        AccentText = Hex(0x409CFF), AccentFill = Hex(0x0055AA),
        StatusSuccess = Hex(0x30DB5B), StatusWarning = Hex(0xFFB340), StatusError = Hex(0xFF6961),
        ToggleOff = Hex(0x636366), WindowControlInactive = Hex(0x636366),
        WindowClose = Hex(0xFF6961), WindowMinimize = Hex(0xFFD426), WindowMaximize = Hex(0x30DB5B),
        WindowControlGlyph = Hex(0x000000),
        SyntaxString = Hex(0x30DB5B), SyntaxNumber = Hex(0xFFB340), SyntaxKeyword = Hex(0xDA8FFF), SyntaxType = Hex(0x5DE6FF),

        RegularTintFloor = 0.75f, ThickTintFloor = 0.90f,
        DrawsOutline = true, HasShadows = false,
    };

    private static JarvisAppearance _appearance = JarvisAppearance.Dark;

    /// <summary>The appearance the app is showing. Setting it raises <see cref="Changed"/>; the
    /// app's JarvisTheme is what sets it (from the user's setting) and swaps the XAML brushes.</summary>
    public static JarvisAppearance Appearance
    {
        get => _appearance;
        set
        {
            if (_appearance == value) return;
            _appearance = value;
            Changed?.Invoke();
        }
    }

    public static JarvisColors Current => For(_appearance);

    public static JarvisColors For(JarvisAppearance appearance) => appearance switch
    {
        JarvisAppearance.Light => Light,
        JarvisAppearance.HighContrast => HighContrast,
        _ => Dark,
    };

    /// <summary>Raised on the UI thread after <see cref="Appearance"/> changes; glass slabs and
    /// controls re-publish so the renderer picks up the new tints.</summary>
    public static event Action? Changed;

    // --- material amounts that don't vary by appearance (tokens: material family) ---

    /// <summary>glass-accent-tint: tint toward <see cref="JarvisColors.AccentFill"/> for the
    /// primary button, the segmented puck and the user's bubble.</summary>
    public const float AccentTintAmount = 0.80f;

    public static Color Hex(uint rgb, byte alpha = 0xFF) =>
        Color.FromArgb(alpha, (byte)(rgb >> 16), (byte)(rgb >> 8), (byte)rgb);

    public static Vector3 ToVector3(Color c) => new(c.R / 255f, c.G / 255f, c.B / 255f);

    /// <summary><paramref name="fg"/> composited over opaque <paramref name="bg"/>: for renderer
    /// shapes that paint opaque (toggle track, slider rail) from a translucent token.</summary>
    public static Vector3 Over(Color fg, Color bg)
    {
        var a = fg.A / 255f;
        return Vector3.Lerp(ToVector3(bg), ToVector3(fg), a);
    }

    public static Vector4 ToVector4(Color c) => new(c.R / 255f, c.G / 255f, c.B / 255f, c.A / 255f);
}

/// <summary>One appearance's colours. See <see cref="JarvisPalette"/>.</summary>
public sealed class JarvisColors
{
    // Base palette: iOS system colours (high contrast: their increased-contrast variants).
    public Color Blue { get; init; }
    public Color Green { get; init; }
    public Color Orange { get; init; }
    public Color Red { get; init; }
    public Color Yellow { get; init; }
    public Color Purple { get; init; }
    public Color Teal { get; init; }
    public Color Gray { get; init; }

    // Text and surfaces.
    public Color Label { get; init; }
    public Color LabelSecondary { get; init; }
    /// <summary>Disabled text and decoration only (3.3:1); never text the user has to read.</summary>
    public Color LabelTertiary { get; init; }
    public Color Placeholder { get; init; }
    public Color GlassTint { get; init; }
    public Color SurfaceCode { get; init; }
    public Color SurfaceAvatar { get; init; }
    public Color FillPrimary { get; init; }
    public Color FillSecondary { get; init; }
    public Color FillTertiary { get; init; }
    public Color Separator { get; init; }
    public Color Outline { get; init; }
    public Color Scrim { get; init; }

    // Accent.
    public Color Accent => Blue;
    public Color AccentText { get; init; }
    public Color AccentFill { get; init; }
    public Color OnAccent { get; init; } = JarvisPalette.Hex(0xFFFFFF);
    public Color FocusRing => AccentText;

    // Status (always with a word and an icon).
    public Color StatusSuccess { get; init; }
    public Color StatusWarning { get; init; }
    public Color StatusError { get; init; }
    public Color StatusInfo => AccentText;
    public Color StatusNeutral => LabelSecondary;

    // Controls.
    public Color ToggleOn => Green;
    public Color ToggleOff { get; init; }
    public Color Thumb { get; init; } = JarvisPalette.Hex(0xFFFFFF);

    // The main window's traffic-light buttons (macOS colours, not the iOS system red/yellow/
    // green: those read as status). Grey while another window is in front.
    public Color WindowClose { get; init; } = JarvisPalette.Hex(0xFF5F57);
    public Color WindowMinimize { get; init; } = JarvisPalette.Hex(0xFEBC2E);
    public Color WindowMaximize { get; init; } = JarvisPalette.Hex(0x28C840);
    public Color WindowControlInactive { get; init; }
    public Color WindowControlGlyph { get; init; } = JarvisPalette.Hex(0x000000, 0x99);

    // Meters.
    public Color MeterNormal => Accent;
    public Color MeterHigh => StatusWarning;
    public Color MeterCritical => StatusError;
    public Color MeterTrack => FillTertiary;

    // Code.
    public Color SyntaxString { get; init; }
    public Color SyntaxNumber { get; init; }
    public Color SyntaxKeyword { get; init; }
    public Color SyntaxType { get; init; }
    public Color SyntaxComment => LabelSecondary;

    // Material floors: a slab's tint never goes below these in this appearance.
    public float RegularTintFloor { get; init; }
    public float ThickTintFloor { get; init; }

    /// <summary>High contrast: a 1px <see cref="Outline"/> on every slab and control.</summary>
    public bool DrawsOutline { get; init; }

    public bool HasShadows { get; init; } = true;

    /// <summary>Every colour token by its XAML resource stem ("LabelSecondary" -> LabelSecondaryBrush).</summary>
    public IEnumerable<(string Name, Color Color)> Tokens()
    {
        yield return (nameof(Blue), Blue);
        yield return (nameof(Green), Green);
        yield return (nameof(Orange), Orange);
        yield return (nameof(Red), Red);
        yield return (nameof(Yellow), Yellow);
        yield return (nameof(Purple), Purple);
        yield return (nameof(Teal), Teal);
        yield return (nameof(Gray), Gray);
        yield return (nameof(Label), Label);
        yield return (nameof(LabelSecondary), LabelSecondary);
        yield return (nameof(LabelTertiary), LabelTertiary);
        yield return (nameof(Placeholder), Placeholder);
        yield return (nameof(GlassTint), GlassTint);
        yield return (nameof(SurfaceCode), SurfaceCode);
        yield return (nameof(SurfaceAvatar), SurfaceAvatar);
        yield return (nameof(FillPrimary), FillPrimary);
        yield return (nameof(FillSecondary), FillSecondary);
        yield return (nameof(FillTertiary), FillTertiary);
        yield return (nameof(Separator), Separator);
        yield return (nameof(Outline), Outline);
        yield return (nameof(Scrim), Scrim);
        yield return (nameof(Accent), Accent);
        yield return (nameof(AccentText), AccentText);
        yield return (nameof(AccentFill), AccentFill);
        yield return (nameof(OnAccent), OnAccent);
        yield return (nameof(FocusRing), FocusRing);
        yield return (nameof(StatusSuccess), StatusSuccess);
        yield return (nameof(StatusWarning), StatusWarning);
        yield return (nameof(StatusError), StatusError);
        yield return (nameof(StatusInfo), StatusInfo);
        yield return (nameof(StatusNeutral), StatusNeutral);
        yield return (nameof(ToggleOn), ToggleOn);
        yield return (nameof(ToggleOff), ToggleOff);
        yield return (nameof(Thumb), Thumb);
        yield return (nameof(WindowClose), WindowClose);
        yield return (nameof(WindowMinimize), WindowMinimize);
        yield return (nameof(WindowMaximize), WindowMaximize);
        yield return (nameof(WindowControlInactive), WindowControlInactive);
        yield return (nameof(WindowControlGlyph), WindowControlGlyph);
        yield return (nameof(MeterNormal), MeterNormal);
        yield return (nameof(MeterHigh), MeterHigh);
        yield return (nameof(MeterCritical), MeterCritical);
        yield return (nameof(MeterTrack), MeterTrack);
        yield return (nameof(SyntaxString), SyntaxString);
        yield return (nameof(SyntaxNumber), SyntaxNumber);
        yield return (nameof(SyntaxKeyword), SyntaxKeyword);
        yield return (nameof(SyntaxType), SyntaxType);
        yield return (nameof(SyntaxComment), SyntaxComment);
    }
}
