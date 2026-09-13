namespace Jarvis_GlassLab;

/// <summary>Which bezel surface curve the shader should evaluate -- kept as an int-castable enum
/// so it can be written straight into the float cbuffer slot DisplacementField.hlsl reads
/// (HLSL has no enum interop; the float value 0/1 is the wire format). Mirrors
/// desktop-winui/Jarvis.Glass/BezelDisplacementMap.cs's BezelProfile on the Win2D side and
/// docs/GLASS_RENDERING.md section 1.</summary>
internal enum GlassBezelProfile
{
    Squircle = 0,
    Lip = 1,
}

/// <summary>
/// CPU-side counterpart to the bezel-profile refraction math in Shaders/DisplacementField.hlsl -
/// used once per profile at startup to compute the normalization constant the shader needs (the
/// max magnitude the per-pixel refraction formula can produce across the bezel depth), the same
/// role BezelDisplacementMap.BuildRefractionLookup's final normalization pass played on the Win2D
/// side, just without needing a full 128-entry lookup table since the shader evaluates the
/// formula directly per pixel.
/// </summary>
internal static class BezelProfileMath
{
    private static readonly float[] Cached =
    {
        ComputeMaxRefractionMagnitude(GlassBezelProfile.Squircle),
        ComputeMaxRefractionMagnitude(GlassBezelProfile.Lip),
    };

    /// <summary>Precomputed per profile -- shapes are built every animation frame, so this must
    /// not re-run the 128-sample scan each time.</summary>
    public static float MaxRefractionMagnitude(GlassBezelProfile profile) => Cached[(int)profile];

    public static float ComputeMaxRefractionMagnitude(GlassBezelProfile profile, float refractiveIndex = 1.5f, int samples = 128)
    {
        var maxAbs = 1e-4f;
        for (var i = 0; i < samples; i++)
        {
            var t = (i + 0.5f) / samples;
            maxAbs = Math.Max(maxAbs, Math.Abs(RefractionMagnitude(t, profile, refractiveIndex)));
        }
        return maxAbs;
    }

    /// <summary>Squircle: h(t) = (1 - (1-t)^4)^(1/4), from the kube.io reference article.</summary>
    private static float SquircleHeight(float t)
    {
        var u = 1f - t;
        var v = Math.Max(0f, 1f - u * u * u * u);
        return MathF.Pow(v, 0.25f);
    }

    /// <summary>Plain convex circle: h(t) = sqrt(1 - (1-t)^2). Half of the Lip blend's two lobes.</summary>
    private static float ConvexCircleHeight(float t)
    {
        var u = 1f - t;
        return MathF.Sqrt(Math.Max(0f, 1f - u * u));
    }

    /// <summary>Concave: h(t) = 1 - sqrt(1 - t^2). The other half of the Lip blend.</summary>
    private static float ConcaveHeight(float t) => 1f - MathF.Sqrt(Math.Max(0f, 1f - t * t));

    /// <summary>kube.io's switch/toggle profile: "the surface convex on the outside and concave
    /// in the middle." A smootherstep blend of a convex outer lobe and a concave inner lobe so
    /// the two meet at t=0.5 with matching value and slope -- a sharp linear blend would show as
    /// a visible kink once run through Snell's law. Same construction as
    /// BezelDisplacementMap.LipProfileHeight on the Win2D side.</summary>
    private static float LipHeight(float t)
    {
        var convex = ConvexCircleHeight(t);
        var concave = ConcaveHeight(t);
        var w = Smootherstep(t);
        return convex * (1f - w) + concave * w;
    }

    private static float Smootherstep(float t)
    {
        t = Math.Clamp(t, 0f, 1f);
        return t * t * t * (t * (t * 6f - 15f) + 10f);
    }

    private static float ProfileHeight(float t, GlassBezelProfile profile) => profile switch
    {
        GlassBezelProfile.Lip => LipHeight(t),
        _ => SquircleHeight(t),
    };

    private static float RefractionMagnitude(float t, GlassBezelProfile profile, float refractiveIndex)
    {
        const float eps = 0.001f;
        var t0 = Math.Clamp(t - eps, 0f, 1f);
        var t1 = Math.Clamp(t + eps, 0f, 1f);
        var h0 = ProfileHeight(t0, profile);
        var h1 = ProfileHeight(t1, profile);
        var dhdt = (h1 - h0) / MathF.Max(1e-4f, t1 - t0);

        var normal = Normalize(-dhdt, -1f);
        const float incidentX = 0f, incidentY = 1f;
        var eta = 1f / refractiveIndex;

        var dot = incidentX * normal.X + incidentY * normal.Y;
        var k = 1f - eta * eta * (1f - dot * dot);
        if (k < 0f)
        {
            return 0f;
        }

        var sqrtK = MathF.Sqrt(k);
        var refractedX = eta * incidentX - (eta * dot + sqrtK) * normal.X;
        var refractedY = eta * incidentY - (eta * dot + sqrtK) * normal.Y;
        var thickness = 1f - t;
        return refractedY > 1e-4f ? refractedX / refractedY * thickness : 0f;
    }

    private static (float X, float Y) Normalize(float x, float y)
    {
        var len = MathF.Sqrt(x * x + y * y);
        return len > 1e-6f ? (x / len, y / len) : (0f, -1f);
    }
}
