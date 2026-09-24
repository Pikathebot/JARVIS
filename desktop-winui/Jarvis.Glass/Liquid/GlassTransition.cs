using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// Moves, scales and fades one element -- its XAML and the glass inside it together -- on damped
/// springs: translation and scale through a CompositeTransform (a render transform, so nothing
/// re-lays out; scale is about the element's centre), opacity through both UIElement.Opacity and
/// <see cref="GlassMotion.OpacityProperty"/>, and the glass's material (frost, tint, shadow)
/// through <see cref="GlassMotion.MaterialProperty"/> on a spring of its own, so glass can arrive
/// clear and condense afterwards. The springs are the controls' house pattern (semi-implicit
/// Euler, 1/240 s substeps, see GlassButton.Spring), parameterised the way Apple's are: a response
/// (the undamped period, s) and a damping ratio (1 = critically damped, no overshoot).
/// Retargeting mid-flight keeps the current velocity, so an interrupted transition bends instead
/// of jumping.
/// </summary>
public sealed class GlassTransition
{
    /// <summary>A spring as SwiftUI states one: <paramref name="Response"/> seconds for one
    /// undamped oscillation, <paramref name="DampingRatio"/> 1 for no overshoot, lower to bounce.</summary>
    public readonly record struct Spring(double Response, double DampingRatio)
    {
        public double Stiffness => Math.Pow(2 * Math.PI / Response, 2);
        public double Damping => 4 * Math.PI * DampingRatio / Response;
    }

    private struct Channel
    {
        public double Value, Velocity, Target;

        public void Step(double dt, Spring spring)
        {
            const double MaxStep = 1.0 / 240.0;
            var (stiffness, damping) = (spring.Stiffness, spring.Damping);
            var steps = Math.Max(1, (int)Math.Ceiling(dt / MaxStep));
            var h = dt / steps;
            for (var i = 0; i < steps; i++)
            {
                var accel = (Target - Value) * stiffness - Velocity * damping;
                Velocity += accel * h;
                Value += Velocity * h;
            }
        }

        public readonly bool Settled(double tolerance) =>
            Math.Abs(Value - Target) < tolerance && Math.Abs(Velocity) < tolerance * 20;

        public void Snap(double to)
        {
            Value = Target = to;
            Velocity = 0;
        }

        public void Settle() => Snap(Target);
    }

    private readonly UIElement _element;
    private readonly CompositeTransform _transform;
    private readonly bool _affectsGlass;
    private Channel _x, _y, _scale, _opacity, _material;
    private Spring _spring = new(0.4, 1.0);
    private Spring _materialSpring = new(0.4, 1.0);
    private double _lastTime = double.NaN;
    private bool _running;
    /// <summary>Bumped whenever a run ends, so a tick left over from a run that <see cref="Set"/>
    /// cut short retires instead of stepping alongside the next run's.</summary>
    private int _run;
    private TaskCompletionSource? _done;

    /// <param name="affectsGlass">False to fade only the XAML (a sheet's backdrop dims the
    /// window's text but must leave its glass panels in place).</param>
    public GlassTransition(UIElement element, bool affectsGlass = true)
    {
        _element = element;
        _affectsGlass = affectsGlass;
        if (element.RenderTransform is CompositeTransform existing)
        {
            _transform = existing;
        }
        else
        {
            _transform = new CompositeTransform();
            element.RenderTransform = _transform;
        }
        _x.Snap(_transform.TranslateX);
        _y.Snap(_transform.TranslateY);
        _scale.Snap(_transform.ScaleX);
        _opacity.Snap(element.Opacity);
        _material.Snap(affectsGlass ? GlassMotion.GetMaterial(element) : 1);
    }

    public double Opacity => _opacity.Value;

    /// <summary>Jumps straight to a state (cancels any animation in flight; its task completes).</summary>
    public void Set(double x, double y, double opacity, double scale = 1, double material = 1)
    {
        _x.Snap(x);
        _y.Snap(y);
        _opacity.Snap(opacity);
        _scale.Snap(scale);
        _material.Snap(material);
        Apply();
        Finish();
        GlassMotion.Notify(_element);
    }

    /// <summary>Springs position, opacity and scale to a state. The task completes when every
    /// channel (material included) settles -- or, fading out, as soon as it is invisible, since
    /// where an invisible element still drifts to doesn't matter.</summary>
    public Task AnimateTo(double x, double y, double opacity, Spring spring, double scale = 1)
    {
        _x.Target = x;
        _y.Target = y;
        _opacity.Target = opacity;
        _scale.Target = scale;
        _spring = spring;
        return Run();
    }

    /// <summary>Springs the glass's material (1 = frost, tint and shadow as designed, 0 = clear)
    /// on its own spring, independently of <see cref="AnimateTo"/>.</summary>
    public Task AnimateMaterial(double material, Spring spring)
    {
        _material.Target = material;
        _materialSpring = spring;
        return Run();
    }

    private Task Run()
    {
        _done ??= new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var task = _done.Task;
        if (!_running)
        {
            _running = true;
            _lastTime = double.NaN;
            var run = _run;
            GlassMotion.Frame(_element, now => run == _run && Tick(now));
        }
        return task;
    }

    private bool Tick(double now)
    {
        // First frame: no dt yet. Apply the start state so the element is where it begins.
        var dt = double.IsNaN(_lastTime) ? 0 : Math.Clamp(now - _lastTime, 0.001, 0.05);
        _lastTime = now;
        if (dt > 0)
        {
            _x.Step(dt, _spring);
            _y.Step(dt, _spring);
            _scale.Step(dt, _spring);
            _opacity.Step(dt, _spring);
            _material.Step(dt, _materialSpring);
        }

        var hidden = _opacity.Target <= 0 && _opacity.Value < 0.01;
        if (hidden || (_x.Settled(0.25) && _y.Settled(0.25) && _scale.Settled(0.0005)
                       && _opacity.Settled(0.002) && _material.Settled(0.002)))
        {
            _x.Settle();
            _y.Settle();
            _scale.Settle();
            _opacity.Settle();
            _material.Settle();
            Apply();
            Finish();
            return false;
        }
        Apply();
        return true;
    }

    private void Apply()
    {
        _transform.TranslateX = _x.Value;
        _transform.TranslateY = _y.Value;
        if (_element is FrameworkElement fe)
        {
            _transform.CenterX = fe.ActualWidth / 2;
            _transform.CenterY = fe.ActualHeight / 2;
        }
        _transform.ScaleX = _transform.ScaleY = _scale.Value;
        // A damped spring can overshoot; opacity and material cannot.
        var opacity = Math.Clamp(_opacity.Value, 0, 1);
        _element.Opacity = opacity;
        if (_affectsGlass)
        {
            GlassMotion.SetOpacity(_element, opacity);
            GlassMotion.SetMaterial(_element, Math.Clamp(_material.Value, 0, 1));
        }
    }

    private void Finish()
    {
        _running = false;
        _run++;
        var done = _done;
        _done = null;
        done?.TrySetResult();
    }
}
