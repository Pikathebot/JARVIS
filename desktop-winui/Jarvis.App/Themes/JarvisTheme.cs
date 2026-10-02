using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace Jarvis_App.Themes;

/// <summary>
/// Puts the design system's colours (<see cref="JarvisPalette"/>) into XAML and switches
/// appearance. Every colour token becomes a <c>{Name}Brush</c> and a <c>{Name}Color</c> in the
/// app's theme dictionaries -- "Dark", "Light", and "HighContrast" (used by WinUI itself when
/// Windows' own high-contrast mode is on) -- so markup says <c>{ThemeResource LabelSecondaryBrush}</c>
/// and never a literal.
///
/// Jarvis's own high-contrast appearance is not a WinUI theme (a window can only request Light
/// or Dark), so it runs on the Dark dictionary with that dictionary's brushes recoloured in place;
/// every element holding one of those brushes follows. Light is a real WinUI theme: windows'
/// roots get <see cref="FrameworkElement.RequestedTheme"/>, and later per-slab flipping can use
/// the same mechanism on a subtree.
/// </summary>
public static class JarvisTheme
{
    private const string SettingKey = "Appearance";

    private static ResourceDictionary _dark = null!;
    // A twin of _dark for the "Default" key: WinUI refuses one dictionary under two keys.
    private static ResourceDictionary _default = null!;
    private static ResourceDictionary _light = null!;
    private static readonly List<WeakReference<Window>> AttachedWindows = new();

    /// <summary>Raised after an appearance change has been applied, for code-built content
    /// (markdown, tool rows) that took brushes at creation time and should rebuild.</summary>
    public static event Action? Applied;

    /// <summary>Adds the theme dictionaries to the application's resources and restores the
    /// saved appearance. Call from OnLaunched, before any window is built.</summary>
    public static void Install(Application app)
    {
        _dark = Build(JarvisPalette.Dark, "jarvis-mark.svg");
        _default = Build(JarvisPalette.Dark, "jarvis-mark.svg");
        _light = Build(JarvisPalette.Light, "jarvis-mark-on-light.svg");
        var themes = new ResourceDictionary();
        themes.ThemeDictionaries["Dark"] = _dark;
        themes.ThemeDictionaries["Default"] = _default;
        themes.ThemeDictionaries["Light"] = _light;
        themes.ThemeDictionaries["HighContrast"] = Build(JarvisPalette.HighContrast, "jarvis-mark.svg");
        app.Resources.MergedDictionaries.Add(themes);
        // After the colours: Typography's styles resolve {ThemeResource LabelBrush} etc. from them.
        app.Resources.MergedDictionaries.Add(new ResourceDictionary { Source = new Uri("ms-appx:///Themes/Typography.xaml") });

        var appearance = SystemHighContrast() ? JarvisAppearance.HighContrast : Saved();
        if (appearance == JarvisAppearance.HighContrast) RecolorDark(JarvisPalette.HighContrast);
        JarvisPalette.Appearance = appearance;
    }

    /// <summary>Gives a window's root the current appearance and keeps it in step with changes.</summary>
    public static void Attach(Window window)
    {
        AttachedWindows.Add(new WeakReference<Window>(window));
        ApplyTo(window);
    }

    public static JarvisAppearance Appearance => JarvisPalette.Appearance;

    /// <summary>Switches appearance live and remembers it.</summary>
    public static void Set(JarvisAppearance appearance)
    {
        if (appearance == JarvisPalette.Appearance) return;
        RecolorDark(appearance == JarvisAppearance.HighContrast ? JarvisPalette.HighContrast : JarvisPalette.Dark);
        JarvisPalette.Appearance = appearance; // slabs re-publish themselves
        foreach (var weak in AttachedWindows)
        {
            if (weak.TryGetTarget(out var window)) ApplyTo(window);
        }
        GlassToggle.MaterialChanged(); // every glass control re-publishes with the new colours
        try
        {
            global::Windows.Storage.ApplicationData.Current.LocalSettings.Values[SettingKey] = appearance.ToString();
        }
        catch
        {
            // Unpackaged runs have no LocalSettings; the choice then lasts for this session.
        }
        Applied?.Invoke();
    }

    /// <summary>The brush for a colour token in the current appearance, for elements built in
    /// code. XAML should use <c>{ThemeResource}</c> instead, which follows changes on its own.</summary>
    public static Brush Brush(string token) =>
        (Brush)(JarvisPalette.Appearance == JarvisAppearance.Light ? _light : _dark)[token + "Brush"];

    /// <summary>A text or icon style from Typography.xaml, for elements built in code. Its
    /// colour setter is a ThemeResource, so it follows appearance changes.</summary>
    public static Style Style(string key) => (Style)Application.Current.Resources[key];

    /// <summary>A fresh brush of a token's colour at a different opacity (badge fills: the
    /// status colour at 14%). Built from the current appearance; not live.</summary>
    public static SolidColorBrush Tinted(Color color, double opacity) =>
        new(Color.FromArgb((byte)Math.Round(255 * opacity), color.R, color.G, color.B));

    public static JarvisColors Colors => JarvisPalette.Current;

    private static void ApplyTo(Window window)
    {
        try
        {
            if (window.Content is FrameworkElement root)
            {
                root.RequestedTheme = JarvisPalette.Appearance == JarvisAppearance.Light ? ElementTheme.Light : ElementTheme.Dark;
            }
        }
        catch
        {
            // a closed window (Settings) still in the weak list: nothing to theme
        }
    }

    private static ResourceDictionary Build(JarvisColors colors, string mark)
    {
        var dictionary = new ResourceDictionary
        {
            // The mark: jarvis-mark on dark grounds, jarvis-mark-on-light on light glass.
            ["MarkSource"] = new Microsoft.UI.Xaml.Media.Imaging.SvgImageSource(new Uri("ms-appx:///Assets/Brand/" + mark)),
        };
        foreach (var (name, color) in colors.Tokens())
        {
            dictionary[name + "Brush"] = new SolidColorBrush(color);
            dictionary[name + "Color"] = color;
        }
        return dictionary;
    }

    /// <summary>High contrast runs on the dark dictionaries, recoloured in place.</summary>
    private static void RecolorDark(JarvisColors colors)
    {
        Recolor(_dark, colors);
        Recolor(_default, colors);
    }

    private static void Recolor(ResourceDictionary dictionary, JarvisColors colors)
    {
        foreach (var (name, color) in colors.Tokens())
        {
            if (dictionary[name + "Brush"] is SolidColorBrush brush) brush.Color = color;
            dictionary[name + "Color"] = color;
        }
    }

    private static JarvisAppearance Saved()
    {
        try
        {
            return global::Windows.Storage.ApplicationData.Current.LocalSettings.Values[SettingKey] is string saved
                && Enum.TryParse<JarvisAppearance>(saved, out var appearance)
                ? appearance
                : JarvisAppearance.Dark;
        }
        catch
        {
            return JarvisAppearance.Dark;
        }
    }

    /// <summary>Windows' own high-contrast mode is on.</summary>
    public static bool SystemHighContrast()
    {
        try
        {
            return new global::Windows.UI.ViewManagement.AccessibilitySettings().HighContrast;
        }
        catch
        {
            return false;
        }
    }
}
