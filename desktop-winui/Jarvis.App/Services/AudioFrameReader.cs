using System.Runtime.InteropServices;
using Windows.Media;
using WinRT;

namespace Jarvis_App.Services;

/// <summary>
/// AudioFrame's IMemoryBuffer doesn't expose a managed byte pointer directly — reading raw
/// samples out of an AudioGraph frame requires QI'ing its buffer reference for this classic
/// COM interface (the standard, documented pattern for AudioFrame PCM access in C#; there is no
/// higher-level managed API for it).
/// </summary>
[ComImport]
[Guid("5B0D3235-4DBA-4D44-865E-8F1D0E4FD04D")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal unsafe interface IMemoryBufferByteAccess
{
    void GetBuffer(out byte* buffer, out uint capacity);
}

/// <summary>Reads the 32-bit float PCM samples out of one AudioFrame into a managed float[].</summary>
internal static unsafe class AudioFrameReader
{
    private static int _logged;

    public static float[] ReadSamples(AudioFrame frame)
    {
        using var buffer = frame.LockBuffer(Windows.Media.AudioBufferAccessMode.Read);
        using var reference = buffer.CreateReference();

        // A plain cast fails under C#/WinRT ("Invalid cast from 'WinRT.IInspectable'"): the
        // projection's objects are not classic RCWs, so QI for a [ComImport] interface has to go
        // through CsWinRT's own As<T>().
        reference.As<IMemoryBufferByteAccess>().GetBuffer(out var dataInBytes, out var capacityInBytes);
        // Length is what the frame actually holds; capacity is the allocation, which can run past
        // it with zeros. Averaging those in dragged the RMS under the speech threshold.
        var validBytes = Math.Min(buffer.Length, capacityInBytes);
        if (_logged++ < 3) Jarvis_App.App.LogVoice($"frame: length {buffer.Length} bytes, capacity {capacityInBytes}, duration {frame.Duration}");
        var floatCount = (int)(validBytes / sizeof(float));
        var samples = new float[floatCount];
        var floatPtr = (float*)dataInBytes;
        for (var i = 0; i < floatCount; i++)
        {
            samples[i] = floatPtr[i];
        }
        return samples;
    }
}
