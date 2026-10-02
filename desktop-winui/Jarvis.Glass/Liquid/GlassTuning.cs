using System.Globalization;
using System.Text;

namespace Jarvis_Glass;

/// <summary>One live-tunable material constant: a static field on a control's <c>Material</c>
/// class (or <see cref="GlassSlab.Tuning"/>), with the range its slider covers.</summary>
public sealed class GlassKnob
{
    public required string Group { get; init; }

    /// <summary>"GlassToggle.RestAspect" -- the saved key and the name the copied line uses.</summary>
    public required string Key { get; init; }

    public required string Label { get; init; }
    public required float Min { get; init; }
    public required float Max { get; init; }
    public required Func<float> Get { get; init; }
    public required Action<float> Set { get; init; }

    /// <summary>The value compiled into the code, read before anything was applied.</summary>
    public float Default { get; internal set; }

    /// <summary>The C# the value lives in, for "Copy values".</summary>
    public required string Field { get; init; }
}

/// <summary>
/// The material constants the Settings > Developer sliders tune live (PLAN 4.8d), ported from
/// GlassLab's tuning table onto the app's own controls, plus a panel group that scales every
/// <see cref="GlassSlab"/>. Controls read their <c>Material</c> statics on every publish, so a
/// change is a field write and a republish (<see cref="Changed"/>).
///
/// Defaults are captured on first use of this class, so it must be touched before anything is
/// applied -- <see cref="Apply"/> does that itself.
/// </summary>
public static class GlassTuning
{
    public static readonly string[] Groups = { "Lens", "Toggle", "Slider", "Button", "Field", "Panels" };

    public static IReadOnlyList<GlassKnob> Knobs { get; }

    static GlassTuning()
    {
        var knobs = new List<GlassKnob>();

        void Add(string group, string type, string field, string label, float min, float max, Func<float> get, Action<float> set) =>
            knobs.Add(new GlassKnob
            {
                Group = group, Key = $"{type}.{field}", Label = label, Min = min, Max = max, Get = get, Set = set,
                Field = type == "GlassSlab" ? $"GlassSlab.Tuning.{field}" : $"{type}.Material.{field}",
            });

        // Lens: shared by every lifted lens (toggle thumb, slider thumb, segmented pill) and the
        // resting shadows -- all on GlassToggle.Material, which the other controls borrow.
        Add("Lens", "GlassToggle", "ForceLift", "Force lift (debug)", 0f, 1f, () => GlassToggle.Material.ForceLift, v => GlassToggle.Material.ForceLift = v);
        Add("Lens", "GlassToggle", "LiftSpecular", "Lift specular", 0f, 2.5f, () => GlassToggle.Material.LiftSpecular, v => GlassToggle.Material.LiftSpecular = v);
        Add("Lens", "GlassToggle", "LiftEdgeRing", "Lift edge ring", 0f, 1f, () => GlassToggle.Material.LiftEdgeRing, v => GlassToggle.Material.LiftEdgeRing = v);
        Add("Lens", "GlassToggle", "SecondLight", "Second light", 0f, 1f, () => GlassToggle.Material.SecondLight, v => GlassToggle.Material.SecondLight = v);
        Add("Lens", "GlassToggle", "LiftShadow", "Lift shadow", 0f, 0.8f, () => GlassToggle.Material.LiftShadow, v => GlassToggle.Material.LiftShadow = v);
        Add("Lens", "GlassToggle", "LiftShadowRadius", "Lift shadow radius", 0f, 30f, () => GlassToggle.Material.LiftShadowRadius, v => GlassToggle.Material.LiftShadowRadius = v);
        Add("Lens", "GlassToggle", "RestSpecular", "Rest specular", 0f, 1.5f, () => GlassToggle.Material.RestSpecular, v => GlassToggle.Material.RestSpecular = v);
        Add("Lens", "GlassToggle", "RestShadow", "Rest shadow", 0f, 0.8f, () => GlassToggle.Material.RestShadow, v => GlassToggle.Material.RestShadow = v);
        Add("Lens", "GlassToggle", "RestShadowRadius", "Rest shadow radius", 0f, 20f, () => GlassToggle.Material.RestShadowRadius, v => GlassToggle.Material.RestShadowRadius = v);
        Add("Lens", "GlassToggle", "ShadowOffsetY", "Shadow offset Y", 0f, 6f, () => GlassToggle.Material.ShadowOffsetY, v => GlassToggle.Material.ShadowOffsetY = v);

        Add("Toggle", "GlassToggle", "RestAspect", "Rest aspect", 1f, 2f, () => GlassToggle.Material.RestAspect, v => GlassToggle.Material.RestAspect = v);
        Add("Toggle", "GlassToggle", "TrackSpecular", "Track specular", 0f, 2f, () => GlassToggle.Material.TrackSpecular, v => GlassToggle.Material.TrackSpecular = v);
        Add("Toggle", "GlassToggle", "TrackRefraction", "Track refraction", 0f, 10f, () => GlassToggle.Material.TrackRefraction, v => GlassToggle.Material.TrackRefraction = v);
        Add("Toggle", "GlassToggle", "ToggleLiftScale", "Lift scale", 1f, 2.4f, () => GlassToggle.Material.ToggleLiftScale, v => GlassToggle.Material.ToggleLiftScale = v);
        Add("Toggle", "GlassToggle", "ToggleLiftAspect", "Lift aspect", 1f, 2.4f, () => GlassToggle.Material.ToggleLiftAspect, v => GlassToggle.Material.ToggleLiftAspect = v);
        Add("Toggle", "GlassToggle", "ToggleLiftTint", "Lift tint", 0f, 1f, () => GlassToggle.Material.ToggleLiftTint, v => GlassToggle.Material.ToggleLiftTint = v);
        Add("Toggle", "GlassToggle", "ToggleLiftBezelFraction", "Lift bezel fraction", 0f, 1f, () => GlassToggle.Material.ToggleLiftBezelFraction, v => GlassToggle.Material.ToggleLiftBezelFraction = v);
        Add("Toggle", "GlassToggle", "ToggleLiftRefraction", "Lift refraction", -30f, 30f, () => GlassToggle.Material.ToggleLiftRefraction, v => GlassToggle.Material.ToggleLiftRefraction = v);
        Add("Toggle", "GlassToggle", "ToggleLiftChromatic", "Lift chromatic", 0f, 0.4f, () => GlassToggle.Material.ToggleLiftChromatic, v => GlassToggle.Material.ToggleLiftChromatic = v);
        Add("Toggle", "GlassToggle", "ToggleLiftFrost", "Lift frost", 0f, 12f, () => GlassToggle.Material.ToggleLiftFrost, v => GlassToggle.Material.ToggleLiftFrost = v);
        Add("Toggle", "GlassToggle", "ToggleLiftMagnify", "Lift magnify", 0f, 0.5f, () => GlassToggle.Material.ToggleLiftMagnify, v => GlassToggle.Material.ToggleLiftMagnify = v);
        Add("Toggle", "GlassToggle", "ThumbStretch", "Stretch", 0f, 0.05f, () => GlassToggle.Material.ThumbStretch, v => GlassToggle.Material.ThumbStretch = v);

        Add("Slider", "GlassSlider", "ThumbAspect", "Thumb aspect", 0.6f, 2.2f, () => GlassSlider.Material.ThumbAspect, v => GlassSlider.Material.ThumbAspect = v);
        Add("Slider", "GlassSlider", "LiftScale", "Lift scale", 1f, 2.2f, () => GlassSlider.Material.LiftScale, v => GlassSlider.Material.LiftScale = v);
        Add("Slider", "GlassSlider", "LiftAspect", "Lift aspect", 0.8f, 2.2f, () => GlassSlider.Material.LiftAspect, v => GlassSlider.Material.LiftAspect = v);
        Add("Slider", "GlassToggle", "LiftTint", "Lift tint", 0f, 1f, () => GlassToggle.Material.LiftTint, v => GlassToggle.Material.LiftTint = v);
        Add("Slider", "GlassToggle", "LiftRefraction", "Lift refraction", 0f, 40f, () => GlassToggle.Material.LiftRefraction, v => GlassToggle.Material.LiftRefraction = v);
        Add("Slider", "GlassToggle", "LiftBezelFraction", "Lift bezel fraction", 0f, 1f, () => GlassToggle.Material.LiftBezelFraction, v => GlassToggle.Material.LiftBezelFraction = v);
        Add("Slider", "GlassToggle", "LiftChromatic", "Lift chromatic", 0f, 0.5f, () => GlassToggle.Material.LiftChromatic, v => GlassToggle.Material.LiftChromatic = v);
        Add("Slider", "GlassToggle", "LiftBlur", "Lift blur px", 0f, 8f, () => GlassToggle.Material.LiftBlur, v => GlassToggle.Material.LiftBlur = v);
        Add("Slider", "GlassSlider", "RailSpecular", "Rail specular", 0f, 1.5f, () => GlassSlider.Material.RailSpecular, v => GlassSlider.Material.RailSpecular = v);
        Add("Slider", "GlassSlider", "StretchMax", "Stretch max", 0f, 1f, () => GlassSlider.Material.StretchMax, v => GlassSlider.Material.StretchMax = v);

        // Buttons, and the segmented control whose track is a clear button slab.
        Add("Button", "GlassButton", "BezelFraction", "Bezel fraction", 0.1f, 1f, () => GlassButton.Material.BezelFraction, v => GlassButton.Material.BezelFraction = v);
        Add("Button", "GlassButton", "RestRefraction", "Refraction", 0f, 24f, () => GlassButton.Material.RestRefraction, v => GlassButton.Material.RestRefraction = v);
        Add("Button", "GlassButton", "RestTint", "Tint", 0f, 0.6f, () => GlassButton.Material.RestTint, v => GlassButton.Material.RestTint = v);
        Add("Button", "GlassButton", "RestSpecular", "Specular", 0f, 2.5f, () => GlassButton.Material.RestSpecular, v => GlassButton.Material.RestSpecular = v);
        Add("Button", "GlassButton", "RestEdgeRing", "Edge ring", 0f, 1f, () => GlassButton.Material.RestEdgeRing, v => GlassButton.Material.RestEdgeRing = v);
        Add("Button", "GlassButton", "RestShadow", "Shadow", 0f, 0.8f, () => GlassButton.Material.RestShadow, v => GlassButton.Material.RestShadow = v);
        Add("Button", "GlassButton", "HoverTintBoost", "Hover tint boost", 0f, 0.3f, () => GlassButton.Material.HoverTintBoost, v => GlassButton.Material.HoverTintBoost = v);
        Add("Button", "GlassButton", "LiftScale", "Pressed scale", 1f, 1.3f, () => GlassButton.Material.LiftScale, v => GlassButton.Material.LiftScale = v);
        Add("Button", "GlassButton", "LiftRefraction", "Pressed refraction", 0f, 30f, () => GlassButton.Material.LiftRefraction, v => GlassButton.Material.LiftRefraction = v);
        Add("Button", "GlassButton", "LiftTint", "Pressed tint", 0f, 0.8f, () => GlassButton.Material.LiftTint, v => GlassButton.Material.LiftTint = v);
        Add("Button", "GlassSegmented", "LiftScale", "Segmented pill lift", 1f, 1.5f, () => GlassSegmented.Material.LiftScale, v => GlassSegmented.Material.LiftScale = v);
        Add("Button", "GlassSegmented", "LiftRefraction", "Segmented pill refraction", 0f, 30f, () => GlassSegmented.Material.LiftRefraction, v => GlassSegmented.Material.LiftRefraction = v);

        Add("Field", "GlassTextField", "BezelFraction", "Bezel fraction", 0.1f, 1f, () => GlassTextField.Material.BezelFraction, v => GlassTextField.Material.BezelFraction = v);
        Add("Field", "GlassTextField", "RestRefraction", "Refraction", 0f, 20f, () => GlassTextField.Material.RestRefraction, v => GlassTextField.Material.RestRefraction = v);
        Add("Field", "GlassTextField", "RestTint", "Tint", 0f, 0.5f, () => GlassTextField.Material.RestTint, v => GlassTextField.Material.RestTint = v);
        Add("Field", "GlassTextField", "RestSpecular", "Specular", 0f, 2f, () => GlassTextField.Material.RestSpecular, v => GlassTextField.Material.RestSpecular = v);
        Add("Field", "GlassTextField", "RestEdgeRing", "Edge ring", 0f, 1f, () => GlassTextField.Material.RestEdgeRing, v => GlassTextField.Material.RestEdgeRing = v);
        Add("Field", "GlassTextField", "FocusScale", "Focus scale", 1f, 1.15f, () => GlassTextField.Material.FocusScale, v => GlassTextField.Material.FocusScale = v);
        Add("Field", "GlassTextField", "FocusRefraction", "Focus refraction", 0f, 24f, () => GlassTextField.Material.FocusRefraction, v => GlassTextField.Material.FocusRefraction = v);
        Add("Field", "GlassTextField", "FocusTintAmount", "Focus tint", 0f, 0.5f, () => GlassTextField.Material.FocusTintAmount, v => GlassTextField.Material.FocusTintAmount = v);
        Add("Field", "GlassTextField", "FocusEdgeRing", "Focus edge ring", 0f, 1f, () => GlassTextField.Material.FocusEdgeRing, v => GlassTextField.Material.FocusEdgeRing = v);

        // Panels: every slab keeps its own XAML values; these scale all of them at once.
        Add("Panels", "GlassSlab", "FrostScale", "Frost ×", 0f, 3f, () => GlassSlab.Tuning.FrostScale, v => GlassSlab.Tuning.FrostScale = v);
        Add("Panels", "GlassSlab", "TintScale", "Tint ×", 0f, 3f, () => GlassSlab.Tuning.TintScale, v => GlassSlab.Tuning.TintScale = v);
        Add("Panels", "GlassSlab", "RefractionScale", "Refraction ×", 0f, 3f, () => GlassSlab.Tuning.RefractionScale, v => GlassSlab.Tuning.RefractionScale = v);
        Add("Panels", "GlassSlab", "BezelScale", "Bezel width ×", 0f, 3f, () => GlassSlab.Tuning.BezelScale, v => GlassSlab.Tuning.BezelScale = v);
        Add("Panels", "GlassSlab", "SpecularScale", "Specular ×", 0f, 3f, () => GlassSlab.Tuning.SpecularScale, v => GlassSlab.Tuning.SpecularScale = v);
        Add("Panels", "GlassSlab", "ShadowScale", "Shadow ×", 0f, 3f, () => GlassSlab.Tuning.ShadowScale, v => GlassSlab.Tuning.ShadowScale = v);
        Add("Panels", "GlassSlab", "SecondLight", "Second light", 0f, 1f, () => GlassSlab.Tuning.SecondLight, v => GlassSlab.Tuning.SecondLight = v);

        foreach (var knob in knobs) knob.Default = knob.Get();
        Knobs = knobs;
    }

    /// <summary>Republishes every control and slab after values change.</summary>
    public static void Changed()
    {
        GlassToggle.MaterialChanged();
        GlassSlab.RepublishAll();
    }

    public static IEnumerable<GlassKnob> InGroup(string group) => Knobs.Where(k => k.Group == group);

    public static bool IsChanged(GlassKnob knob) => Math.Abs(knob.Get() - knob.Default) > 1e-5f;

    /// <summary>Only the values that differ from the code's -- what gets saved.</summary>
    public static Dictionary<string, float> Changes() =>
        Knobs.Where(IsChanged).ToDictionary(k => k.Key, k => k.Get());

    /// <summary>Sets saved values (unknown keys ignored, each clamped to its range) and republishes.</summary>
    public static void Apply(IReadOnlyDictionary<string, float> values)
    {
        foreach (var knob in Knobs)
        {
            if (values.TryGetValue(knob.Key, out var v) && float.IsFinite(v))
            {
                knob.Set(Math.Clamp(v, knob.Min, knob.Max));
            }
        }
        Changed();
    }

    public static void Reset(string? group = null)
    {
        foreach (var knob in Knobs)
        {
            if (group is null || knob.Group == group) knob.Set(knob.Default);
        }
        Changed();
    }

    /// <summary>The changed values as C# lines, to paste back into the Material classes.</summary>
    public static string Describe()
    {
        var changed = Knobs.Where(IsChanged).ToList();
        if (changed.Count == 0) return "// Glass tuning: everything at its default.";
        var sb = new StringBuilder("// Glass tuning (Settings > Developer)\n");
        foreach (var knob in changed)
        {
            sb.Append(CultureInfo.InvariantCulture,
                $"{knob.Field} = {knob.Get():0.####}f;  // was {knob.Default:0.####}f\n");
        }
        return sb.ToString();
    }
}
