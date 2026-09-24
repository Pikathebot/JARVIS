using Vortice.Direct3D11;

namespace Jarvis_Glass;

/// <summary>
/// Small shared compile/bind helpers, introduced in Phase 4 now that there is more than one
/// shader pass. Deliberately thin - the real content is the shaders themselves and GlassRenderer's
/// orchestration of them, not an abstraction layer.
/// </summary>
internal static class ShaderPipeline
{
    public static ID3D11VertexShader CreateVertexShader(ID3D11Device device, string path, string entryPoint) =>
        device.CreateVertexShader(Bytecode(path, entryPoint, "vs_5_0"));

    public static ID3D11PixelShader CreatePixelShader(ID3D11Device device, string path, string entryPoint) =>
        device.CreatePixelShader(Bytecode(path, entryPoint, "ps_5_0"));

    // Compiled bytecode, keyed by a hash of the source plus entry point and profile (the shaders
    // have no #includes, so the file is the whole input). Compiling from source cost ~0.7 s per
    // GlassHost at launch -- and the app builds two (main + HUD), ~1.55 s of the ~4 s before the
    // window showed. In memory, the second host reuses the first's; on disk, later launches skip
    // the compiler entirely. An edited shader hashes differently and simply compiles again.
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte[]> Compiled = new();

    private static readonly string CacheDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Jarvis", "ShaderCache");

    private static byte[] Bytecode(string path, string entryPoint, string profile)
    {
        var source = File.ReadAllBytes(path);
        var key = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            [.. source, .. System.Text.Encoding.UTF8.GetBytes($"|{entryPoint}|{profile}")]))[..32];
        return Compiled.GetOrAdd(key, k =>
        {
            var file = Path.Combine(CacheDir, k + ".cso");
            try
            {
                if (File.Exists(file)) return File.ReadAllBytes(file);
            }
            catch
            {
                // unreadable cache entry: compile instead
            }
            var bytecode = Vortice.D3DCompiler.Compiler.CompileFromFile(path, entryPoint, profile).ToArray();
            try
            {
                Directory.CreateDirectory(CacheDir);
                var temp = file + "." + Environment.ProcessId + ".tmp";
                File.WriteAllBytes(temp, bytecode);
                File.Move(temp, file, overwrite: true);
            }
            catch
            {
                // best effort: the next launch compiles again
            }
            return bytecode;
        });
    }

    /// <summary>
    /// Constant buffers must be a multiple of 16 bytes to match HLSL's cbuffer packing rules - a
    /// mismatch here is a silent wrong-output bug (fields land at the wrong offset), not a
    /// compile error, which is exactly the failure class this project is trying to avoid by using
    /// raw D3D11/HLSL instead of D2D's PixelShaderEffect. Asserting it here, once, at the point
    /// every constant buffer in this project gets created, is cheaper than re-deriving it by eye
    /// for each cbuffer struct.
    /// </summary>
    public static ID3D11Buffer CreateConstantBuffer(ID3D11Device device, int byteWidth)
    {
        if (byteWidth % 16 != 0)
        {
            throw new ArgumentException(
                $"Constant buffer size {byteWidth} is not 16-byte aligned - it will not match the HLSL cbuffer's packing.",
                nameof(byteWidth));
        }

        return device.CreateBuffer(new BufferDescription
        {
            ByteWidth = (uint)byteWidth,
            Usage = ResourceUsage.Dynamic,
            BindFlags = BindFlags.ConstantBuffer,
            CPUAccessFlags = CpuAccessFlags.Write,
        });
    }
}
