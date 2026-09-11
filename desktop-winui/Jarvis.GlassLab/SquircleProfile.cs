namespace Jarvis_GlassLab;

/// <summary>
/// CPU-side counterpart to the squircle refraction math in Shaders/DisplacementField.hlsl - used
/// once at startup to compute the normalization constant the shader needs (the max magnitude the
/// per-pixel refraction formula can produce across the bezel depth), the same role
/// BezelDisplacementMap.BuildRefractionLookup's final normalization pass played, just without
/// needing a full 128-entry lookup table since the shader evaluates the formula directly.
/// </summary>
internal static class SquircleProfile
{
    public static float ComputeMaxRefractionMagnitude(float refractiveIndex = 1.5f, int samples = 128)
    {
        var maxAbs = 1e-4f;
        for (var i = 0; i < samples; i++)
        {
            var t = (i + 0.5f) / samples;
            maxAbs = Math.Max(maxAbs, Math.Abs(RefractionMagnitude(t, refractiveIndex)));
        }
        return maxAbs;
    }

    private static float ProfileHeight(float t)
    {
        var u = 1f - t;
        var v = Math.Max(0f, 1f - u * u * u * u);
        return MathF.Pow(v, 0.25f);
    }

    private static float RefractionMagnitude(float t, float refractiveIndex)
    {
        const float eps = 0.001f;
        var t0 = Math.Clamp(t - eps, 0f, 1f);
        var t1 = Math.Clamp(t + eps, 0f, 1f);
        var h0 = ProfileHeight(t0);
        var h1 = ProfileHeight(t1);
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
