using Jarvis.Core.Models;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace Jarvis_App;

public sealed class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        value is true ? Visibility.Visible : Visibility.Collapsed;

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        value is Visibility.Visible;
}

/// <summary>Right panel column width: 0 when closed, 360 when open — gives the overlay sheet
/// somewhere to spring open into without a fixed reserved gap when collapsed.</summary>
public sealed class BoolToPanelWidthConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        new GridLength(value is true ? 368 : 0);

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

/// <summary>Activity trace dot color: green=success, red=error, amber=running/other.</summary>
public sealed class ActivityStatusToBrushConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        new SolidColorBrush(Themes.StatusStyle.ColorOf(Themes.StatusStyle.FromStepStatus(value as string)));

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

/// <summary>Awareness severity colour (design system: status tokens).</summary>
public sealed class SeverityToBrushConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        new SolidColorBrush(SeverityColor(value as ObservationSeverity? ?? ObservationSeverity.Info));

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();

    public static Themes.StatusKind KindOf(ObservationSeverity severity) => severity switch
    {
        ObservationSeverity.Critical => Themes.StatusKind.Error,
        ObservationSeverity.Warning => Themes.StatusKind.Warning,
        ObservationSeverity.Notice => Themes.StatusKind.Info,
        _ => Themes.StatusKind.Neutral,
    };

    public static Color SeverityColor(ObservationSeverity severity) => Themes.StatusStyle.ColorOf(KindOf(severity));
}

/// <summary>Severity background tint: the severity colour at 14%.</summary>
public sealed class SeverityToBackgroundConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        Themes.JarvisTheme.Tinted(SeverityToBrushConverter.SeverityColor(value as ObservationSeverity? ?? ObservationSeverity.Info), 0.14);

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}

/// <summary>Severity icon (Segoe Fluent): status is an icon and a word as well as a colour.</summary>
public sealed class SeverityToGlyphConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, string language) =>
        Themes.StatusStyle.GlyphOf(SeverityToBrushConverter.KindOf(value as ObservationSeverity? ?? ObservationSeverity.Info));

    public object ConvertBack(object value, Type targetType, object parameter, string language) =>
        throw new NotSupportedException();
}