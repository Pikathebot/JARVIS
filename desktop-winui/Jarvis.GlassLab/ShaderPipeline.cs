using Vortice.Direct3D11;

namespace Jarvis_GlassLab;

/// <summary>
/// Small shared compile/bind helpers, introduced in Phase 4 now that there is more than one
/// shader pass. Deliberately thin - the real content is the shaders themselves and GlassRenderer's
/// orchestration of them, not an abstraction layer.
/// </summary>
internal static class ShaderPipeline
{
    public static ID3D11VertexShader CreateVertexShader(ID3D11Device device, string path, string entryPoint)
    {
        var bytecode = Vortice.D3DCompiler.Compiler.CompileFromFile(path, entryPoint, "vs_5_0");
        return device.CreateVertexShader(bytecode.Span);
    }

    public static ID3D11PixelShader CreatePixelShader(ID3D11Device device, string path, string entryPoint)
    {
        var bytecode = Vortice.D3DCompiler.Compiler.CompileFromFile(path, entryPoint, "ps_5_0");
        return device.CreatePixelShader(bytecode.Span);
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
