# Liquid Glass Rendering — Technique Reference

Technical reference for the refraction/displacement technique behind Apple-style "liquid glass"
UI, and how it maps onto `desktop-winui/Jarvis.Glass`. Written after evaluating three external
references (2026-09-08) so the approach doesn't need re-deriving next session.

**Status**: reference only — not yet implemented. Supersedes the custom-HLSL approach described
for PLAN.md's "Checkpoint C" (see below); nothing here has been built or verified on-device yet.

## Sources evaluated

- `github.com/winaviation-tweaks/liquidass` — **not applicable**. This is an iOS jailbreak tweak
  (Theos/Logos) that patches Apple's own iOS 26 Liquid Glass system UI on-device via private
  SpringBoard/Backboardd hooks. No rendering technique to reuse (nothing implements the effect;
  it modifies Apple's existing one), no portable code, non-commercial license. Dead end.
- `winaviation.github.io/liquid-glass-demo` and `kube.io/blog/liquid-glass-css-svg` — **the real
  find**, likely the same underlying technique/author. A browser (SVG + CSS) implementation that
  is pure math: nothing here depends on a browser feature Windows can't do, it's a CPU-generated
  lookup bitmap fed into a standard displacement-map compositing primitive. Directly portable.

## The technique

The "glass" look is **not** a live optical simulation per frame. It's a displacement map —
a small RGB bitmap encoding "push this pixel by (dx, dy)" — generated **once** (regenerate only on
resize/profile change) and then applied to whatever sits behind the glass via a standard
displacement-map compositing effect. All the interesting work is in generating that bitmap.

### 1. Bezel profile

A parametric curve `y = f(x)`, `x, y ∈ [0, 1]`, giving glass surface height across the bezel
(the rounded rim), `x = 0` at the outer edge, `x = 1` at the flat interior:

```
convex_circle:   y = sqrt(1 - (1-x)^2)
convex_squircle: y = (1 - (1-x)^4)^(1/4)
concave:         y = 1 - sqrt(1 - x^2)
lip:             smootherstep blend of a convex and concave segment
```

### 2. 1D refraction lookup (Snell's law)

For ~128 samples along the profile:
1. Take the numeric derivative of `f` at each sample to get the surface normal
   `normal = normalize(-derivative, -1)`.
2. Apply the vector form of Snell's law with `eta = 1 / refractive_index` (glass ≈ 1.5) against
   a vertical incident ray:
   ```
   dot = dot(incident, normal)
   k = 1 - eta^2 * (1 - dot^2)
   refracted = eta * incident - (eta * dot + sqrt(k)) * normal
   ```
3. Project the refracted ray through the remaining glass thickness to get a horizontal
   displacement at that depth. Store as a 128-entry lookup table.

This models a single refraction event through a glass bezel of a given profile/thickness/
refractive-index — not a full raytrace, no total internal reflection, orthogonal incident rays
only. That's enough; the visual payoff is disproportionate to the model's simplicity.

### 3. 2D displacement bitmap

For each pixel in the bezel band of a rounded rect (i.e. within `bezel_width` of an edge or
corner):
1. Convert to polar coordinates relative to the nearest corner/edge center: `distance`, `cos`,
   `sin`.
2. Map `distance` into the 1D lookup table by how far into the bezel it sits
   (`bezel_ratio = (radius - distance) / bezel_width`).
3. Encode the resulting displacement vector into RG channels, 128 = zero displacement:
   ```
   dx = -cos * displacement / max_displacement
   dy = -sin * displacement / max_displacement
   R  = 128 + dx * 127 * opacity
   G  = 128 + dy * 127 * opacity
   B  = 0
   ```

### 4. Specular highlight (separate layer, screen-blended)

Same polar/normal derivation, dotted against a fixed light direction (e.g. 60°), with a sharp
falloff concentrated at the rim:
```
dot        = |cos * light.x + sin * light.y|
edge_ratio = (radius - distance) / specular_thickness
falloff    = sqrt(1 - (1 - edge_ratio)^2)
brightness = min(1, dot * falloff)
```
Screen-blend this grayscale/alpha layer over the displaced result.

### 5. Compositing

The browser reference does this with SVG's `<feDisplacementMap>`: `in=SourceGraphic`,
`in2=<the RG bitmap>`, `xChannelSelector="R"`, `yChannelSelector="G"`, `scale=<intensity>`. It's
literally "look up this pixel's displacement from the map, offset the source read by that much."

## Mapping to Win2D / `Jarvis.Glass`

This is the part that makes the research worth keeping: **Win2D already has the exact effect
needed**, so this does not require a custom pixel shader.

- `Microsoft.Graphics.Canvas.Effects.DisplacementMapEffect` is the direct C#/Win2D equivalent of
  `feDisplacementMap` — takes a `Source` image, a `Displacement` image, `Amount`, and
  `XChannelSelect`/`YChannelSelect`. Chain it the same way `LiquidGlassCanvas` already chains
  `GaussianBlurEffect`.
- The bezel-profile + Snell's-law lookup table and the 2D bitmap rasterization port directly to
  C# — it's arithmetic over a `CanvasRenderTarget`/`byte[]`, no browser-specific API involved.
  Generate once per (corner radius, bezel width, profile) combination and cache the
  `CanvasBitmap`, same pattern `WallpaperBitmapCache` already uses for the wallpaper crop.
  Regenerate only on resize or profile/parameter change, never per frame.
- The specular layer generation ports the same way, and can screen-blend onto the existing
  pointer-tracked sheen already in `LiquidGlassCanvas.DrawBody`, or replace it.

**This replaces the PLAN.md "Checkpoint C" custom-HLSL plan** (`EdgeRefraction.hlsl` compiled via
`fxc.exe`, wrapped in `PixelShaderEffect`). That approach carried real risk flagged in
`winui_migration_status` memory — a shader constant-buffer/D2D-entry-point mismatch fails
*silently* (garbage output, not a compile error), which is hard to diagnose. Building the
displacement map on the CPU/canvas side and feeding a stock Win2D effect avoids the HLSL
compile-and-bind step entirely, at the cost of an offscreen bitmap generation pass (cheap, since
it's cached and only regenerated on resize).

Land after Checkpoints A (live capture) and B (parallax), same as originally planned — this is a
drop-in replacement for Checkpoint C's *mechanism*, not its position in the sequence. Also carries
forward the existing caveat: on this machine there's no sampleable real backdrop (Wallpaper Engine
renders behind DWM/the wallpaper file), so displacement will refract the designed gradient body,
not real desktop content, until/unless the live-capture backdrop (Checkpoint A) supplies real
detail to bend.

See [[winui_migration_status]] and [[jarvis_architecture]] memories for how this connects to the
rest of the WinUI migration.
