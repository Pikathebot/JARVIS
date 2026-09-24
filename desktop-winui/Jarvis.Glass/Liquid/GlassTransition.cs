using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// Moves and fades one element -- its XAML and the glass inside it together -- on damped
/// springs: translation through a TranslateTransform (a render transform, so nothing re-lays
/// out) and opacity through both UIElement.Opacity and <see cref="GlassMotion.OpacityProperty"/>.
/// The springs are the controls' house pattern (semi-implicit Euler, 1/240 s substeps, see
/// GlassButton.Spring), parameterised the way Apple's are: a response (the undamped period, s)
/// and a damping ratio (1 = critically damped, no overshoot). Retargeting mid-flight keeps the
/// current velocity, so an interrupted transition bends instead of jumping.
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

        public void Step(double dt, double stiffness, double damping)
        {
            const double MaxStep = 1.0 / 240.0;
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
    }

    private readonly UIElement _element;
    private readonly TranslateTransform _translate;
    private readonly bool _affectsGlass;
    private Channel _x, _y, _opacity;
    private Spring _spring = new(0.4, 1.0);
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
        if (element.RenderTransform is TranslateTransform existing)
        {
            _translate = existing;
        }
        else
        {
            _translate = new TranslateTransform();
            element.RenderTransform = _translate;
        }
        _x.Snap(_translate.X);
        _y.Snap(_translate.Y);
        _opacity.Snap(element.Opacity);
    }

    public double Opacity => _opacity.Value;

    /// <summary>Jumps straight to a state (cancels any animation in flight; its task completes).</summary>
    public void Set(double x, double y, double opacity)
    {
        _x.Snap(x);
        _y.Snap(y);
        _opacity.Snap(opacity);
        Apply();
        Finish();
        GlassMotion.Notify(_element);
    }

    /// <summary>Springs to a state. The task completes when it settles -- or, fading out, as soon
    /// as it is invisible, since where an invisible element still drifts to doesn't matter.</summary>
    public Task AnimateTo(double x, double y, double opacity, Spring spring)
    {
        _x.Target = x;
        _y.Target = y;
        _opacity.Target = opacity;
        _spring = spring;
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
            var (k, c) = (_spring.Stiffness, _spring.Damping);
            _x.Step(dt, k, c);
            _y.Step(dt, k, c);
            _opacity.Step(dt, k, c);
        }

        var hidden = _opacity.Target <= 0 && _opacity.Value < 0.01;
        if (hidden || (_x.Settled(0.25) && _y.Settled(0.25) && _opacity.Settled(0.002)))
        {
            _x.Snap(_x.Target);
            _y.Snap(_y.Target);
            _opacity.Snap(_opacity.Target);
            Apply();
            Finish();
            return false;
        }
        Apply();
        return true;
    }

    private void Apply()
    {
        _translate.X = _x.Value;
        _translate.Y = _y.Value;
        // A damped spring can overshoot; opacity cannot.
        var opacity = Math.Clamp(_opacity.Value, 0, 1);
        _element.Opacity = opacity;
        if (_affectsGlass) GlassMotion.SetOpacity(_element, opacity);
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
