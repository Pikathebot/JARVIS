using Microsoft.Graphics.Canvas;
using Windows.Graphics.DirectX;

namespace Jarvis_Glass;

/// <summary>
/// Generates the RG displacement bitmap that <see cref="LiquidGlassCanvas"/> feeds into Win2D's
/// <c>DisplacementMapEffect</c> to bend the backdrop near a panel's rounded edge -- the technique
/// documented in docs/GLASS_RENDERING.md: a bezel profile run through Snell's law produces a 1D
/// lookup table of horizontal displacement by depth into the bezel, which is then rasterized into a
/// 2D map via the panel's rounded-rect signed distance field. This is the same underlying math as
/// SVG's feDisplacementMap-based liquid-glass demos; DisplacementMapEffect is Win2D's direct
/// equivalent, so no custom pixel shader is needed.
///
/// Shaped like <c>GrainBitmap</c> (cached generated bitmap, regenerated only when inputs change) but
/// keyed per panel size/radius/bezel-width, since -- unlike the grain tile -- the map is not
/// resolution-independent. Bounded cache: panels resize on window drag/resize, not continuously, so
/// eviction past a small cap keeps this from growing unbounded over a long session.
/// </summary>
internal static class BezelDisplacementMap
{
    private const int LookupSamples = 128;
    private const int MaxCacheEntries = 24;

    private static readonly Dictionary<(int Width, int Height, int Radius, int BezelWidth), CanvasBitmap> _cache = new();
    private static readonly List<(int Width, int Height, int Radius, int BezelWidth)> _cacheOrder = new();

    /// <summary>
    /// Builds the 1D horizontal-displacement lookup table for a convex-circle bezel profile
    /// (<c>y = sqrt(1 - (1-x)^2)</c>), <paramref name="samples"/> entries across the bezel depth
    /// (index 0 = outer edge, last index = flat interior). Cheap enough to build fresh per call --
    /// callers should still cache the result across draws since it depends only on the fixed
    /// refractive index used app-wide.
    /// </summary>
    public static float[] BuildRefractionLookup(float refractiveIndex = 1.5f, int samples = LookupSamples)
    {
        var lookup = new float[samples];
        var eta = 1.0f / refractiveIndex;

        for (var i = 0; i < samples; i++)
        {
            var x = (i + 0.5f) / samples;

            // Numeric derivative of the convex-circle profile y = sqrt(1 - (1-x)^2).
            const float h = 0.001f;
            var x0 = Math.Clamp(x - h, 0f, 1f);
            var x1 = Math.Clamp(x + h, 0f, 1f);
            var y0 = ProfileHeight(x0);
            var y1 = ProfileHeight(x1);
            var dydx = (y1 - y0) / Math.Max(1e-4f, x1 - x0);

            // Surface normal from the profile slope, incident ray straight down (0, -1).
            var normal = Normalize(-dydx, -1f);
            const float incidentX = 0f, incidentY = 1f; // ray travelling in +y (into the surface)

            var dot = incidentX * normal.X + incidentY * normal.Y;
            var k = 1f - eta * eta * (1f - dot * dot);

            float dx;
            if (k < 0f)
            {
                // Total internal reflection -- not physically meaningful for a single entry
                // refraction from air into glass at these angles, but guard it anyway so a steep
                // profile can never produce NaN.
                dx = 0f;
            }
            else
            {
                var sqrtK = MathF.Sqrt(k);
                var refractedX = eta * incidentX - (eta * dot + sqrtK) * normal.X;
                var refractedY = eta * incidentY - (eta * dot + sqrtK) * normal.Y;
                // Project through the remaining glass thickness (1 - x, in profile units) to get
                // the accumulated horizontal displacement at this depth.
                var thickness = 1f - x;
                dx = refractedY > 1e-4f ? refractedX / refractedY * thickness : 0f;
            }

            lookup[i] = dx;
        }

        // Normalize to [-1, 1] so callers scale by their own max-displacement-in-pixels.
        var maxAbs = 1e-4f;
        foreach (var v in lookup) maxAbs = Math.Max(maxAbs, Math.Abs(v));
        for (var i = 0; i < samples; i++) lookup[i] /= maxAbs;

        return lookup;
    }

    private static float ProfileHeight(float x)
    {
        var t = 1f - x;
        return MathF.Sqrt(Math.Max(0f, 1f - t * t));
    }

    private static (float X, float Y) Normalize(float x, float y)
    {
        var len = MathF.Sqrt(x * x + y * y);
        return len > 1e-6f ? (x / len, y / len) : (0f, -1f);
    }

    /// <summary>
    /// Returns the cached displacement bitmap for this exact (size, radius, bezel-width)
    /// combination, generating and caching it on a miss. Sizes are rounded to whole pixels before
    /// lookup, so sub-pixel layout churn during a drag doesn't thrash the cache.
    /// </summary>
    public static CanvasBitmap? Get(ICanvasResourceCreator device, int width, int height, float radius, float bezelWidth, float[] lookup)
    {
        if (width <= 0 || height <= 0) return null;

        var key = (width, height, (int)MathF.Round(radius), (int)MathF.Round(bezelWidth));
        if (_cache.TryGetValue(key, out var cached)) return cached;

        CanvasBitmap bitmap;
        try
        {
            bitmap = Generate(device, width, height, radius, bezelWidth, lookup);
        }
        catch
        {
            // Any failure here (out of memory generating a huge bitmap, device loss mid-generate)
            // should fall back to "no displacement" rather than take the whole panel draw down.
            return null;
        }

        _cache[key] = bitmap;
        _cacheOrder.Add(key);
        if (_cacheOrder.Count > MaxCacheEntries)
        {
            var oldest = _cacheOrder[0];
            _cacheOrder.RemoveAt(0);
            if (_cache.Remove(oldest, out var evicted)) evicted.Dispose();
        }

        return bitmap;
    }

    private static CanvasBitmap Generate(ICanvasResourceCreator device, int width, int height, float radius, float bezelWidth, float[] lookup)
    {
        var pixels = new byte[width * height * 4];
        var halfW = width / 2f;
        var halfH = height / 2f;
        var r = Math.Min(radius, Math.Min(halfW, halfH));
        bezelWidth = Math.Max(1f, Math.Min(bezelWidth, r));

        for (var py = 0; py < height; py++)
        {
            for (var px = 0; px < width; px++)
            {
                var idx = (py * width + px) * 4;

                // Rounded-rect signed distance field, centered at the panel's own center.
                var x = px + 0.5f - halfW;
                var y = py + 0.5f - halfH;
                var qx = Math.Abs(x) - (halfW - r);
                var qy = Math.Abs(y) - (halfH - r);
                var outsideX = Math.Max(qx, 0f);
                var outsideY = Math.Max(qy, 0f);
                var sdf = MathF.Sqrt(outsideX * outsideX + outsideY * outsideY) + Math.Min(Math.Max(qx, qy), 0f) - r;

                if (sdf > 0f || sdf < -bezelWidth)
                {
                    // Outside the panel, or past the bezel band into the flat interior: zero
                    // displacement. DisplacementMapEffect leaves these pixels untouched.
                    pixels[idx + 0] = 0;   // B
                    pixels[idx + 1] = 128; // G -> Y
                    pixels[idx + 2] = 128; // R -> X
                    pixels[idx + 3] = 255; // A
                    continue;
                }

                var t = Math.Clamp(1f - -sdf / bezelWidth, 0f, 1f); // 1 at outer edge, 0 at bezel's inner boundary
                var sampleIndex = Math.Clamp((int)(t * (lookup.Length - 1)), 0, lookup.Length - 1);
                var displacement = lookup[sampleIndex];

                // Outward normal via central-difference gradient of the SDF -- cheap and accurate
                // enough at pixel resolution; avoids deriving a separate closed-form normal per
                // region (corner vs. edge).
                var (nx, ny) = SdfGradient(x, y, halfW, halfH, r);

                var dx = -nx * displacement;
                var dy = -ny * displacement;

                // B8G8R8A8 byte order is Blue,Green,Red,Alpha -- X (dx) belongs in the Red channel
                // and must land at byte index 2, not 0, or XChannelSelect.Red silently reads the
                // constant Blue byte instead of the per-pixel data.
                pixels[idx + 0] = 0;                                          // B (unused)
                pixels[idx + 1] = (byte)Math.Clamp(128 + dy * 127, 0, 255);   // G -> Y displacement
                pixels[idx + 2] = (byte)Math.Clamp(128 + dx * 127, 0, 255);   // R -> X displacement
                pixels[idx + 3] = 255;                                        // A
            }
        }

        return CanvasBitmap.CreateFromBytes(device, pixels, width, height, DirectXPixelFormat.B8G8R8A8UIntNormalized);
    }

    private static (float X, float Y) SdfGradient(float x, float y, float halfW, float halfH, float r)
    {
        const float eps = 0.75f;
        var d0 = RoundRectSdf(x - eps, y, halfW, halfH, r);
        var d1 = RoundRectSdf(x + eps, y, halfW, halfH, r);
        var d2 = RoundRectSdf(x, y - eps, halfW, halfH, r);
        var d3 = RoundRectSdf(x, y + eps, halfW, halfH, r);
        return Normalize(d1 - d0, d3 - d2);
    }

    private static float RoundRectSdf(float x, float y, float halfW, float halfH, float r)
    {
        var qx = Math.Abs(x) - (halfW - r);
        var qy = Math.Abs(y) - (halfH - r);
        var outsideX = Math.Max(qx, 0f);
        var outsideY = Math.Max(qy, 0f);
        return MathF.Sqrt(outsideX * outsideX + outsideY * outsideY) + Math.Min(Math.Max(qx, qy), 0f) - r;
    }
}
