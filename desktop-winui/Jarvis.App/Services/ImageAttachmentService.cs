using System;
using System.Collections.Generic;
using System.IO;
using System.Threading.Tasks;
using Windows.ApplicationModel.DataTransfer;
using Windows.Graphics.Imaging;
using Windows.Storage;
using Windows.Storage.Streams;

namespace Jarvis_App.Services;

/// <summary>
/// Gets images into a shape worth sending to a local vision model.
///
/// Qwen3.5's projector uses dynamic resolution, so a 4K screenshot costs thousands of
/// context tokens for no gain in what the model can read from it. Anything with a longest
/// side over <see cref="MaxEdge"/> is downscaled to a JPEG in the temp folder before upload
/// and the original never leaves the machine untouched. Clipboard bitmaps (a Win+Shift+S
/// capture) are saved the same way so they can be attached with Ctrl+V.
/// </summary>
public static class ImageAttachmentService
{
    /// <summary>Longest side after downscaling. Roughly the point past which the projector's
    /// token cost keeps growing but a screenshot's legibility does not.</summary>
    public const int MaxEdge = 1568;

    private static readonly HashSet<string> ImageExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    };

    public static bool IsImagePath(string path) =>
        ImageExtensions.Contains(Path.GetExtension(path));

    private static string TempDir
    {
        get
        {
            var dir = Path.Combine(Path.GetTempPath(), "jarvis-images");
            Directory.CreateDirectory(dir);
            return dir;
        }
    }

    /// <summary>
    /// The path to upload for an image: the original when it is already small enough, else a
    /// downscaled JPEG copy. Never throws — a file that cannot be decoded is uploaded as-is and
    /// the backend's captioner or projector deals with it.
    /// </summary>
    public static async Task<string> PrepareForUploadAsync(string path)
    {
        if (!IsImagePath(path)) return path;
        try
        {
            var file = await StorageFile.GetFileFromPathAsync(path);
            using var input = await file.OpenAsync(FileAccessMode.Read);
            var decoder = await BitmapDecoder.CreateAsync(input);
            var width = decoder.PixelWidth;
            var height = decoder.PixelHeight;
            var longest = Math.Max(width, height);
            if (longest <= MaxEdge) return path;

            var scale = MaxEdge / (double)longest;
            var target = Path.Combine(TempDir, $"{Path.GetFileNameWithoutExtension(path)}-{Guid.NewGuid():N}.jpg");
            var outFile = await StorageFile.GetFileFromPathAsync(await CreateEmptyAsync(target));
            using var output = await outFile.OpenAsync(FileAccessMode.ReadWrite);
            var encoder = await BitmapEncoder.CreateForTranscodingAsync(output, decoder);
            encoder.BitmapTransform.ScaledWidth = (uint)Math.Max(1, Math.Round(width * scale));
            encoder.BitmapTransform.ScaledHeight = (uint)Math.Max(1, Math.Round(height * scale));
            encoder.BitmapTransform.InterpolationMode = BitmapInterpolationMode.Fant;
            await encoder.FlushAsync();
            return target;
        }
        catch
        {
            return path;
        }
    }

    /// <summary>
    /// If the clipboard holds a bitmap, save it as a PNG in the temp folder and return the path;
    /// null when there is no image on the clipboard.
    /// </summary>
    public static async Task<string?> SaveClipboardImageAsync()
    {
        try
        {
            var content = Clipboard.GetContent();
            if (!content.Contains(StandardDataFormats.Bitmap)) return null;
            var reference = await content.GetBitmapAsync();
            using var source = await reference.OpenReadAsync();
            var decoder = await BitmapDecoder.CreateAsync(source);
            var pixels = await decoder.GetPixelDataAsync(
                BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
                new BitmapTransform(), ExifOrientationMode.IgnoreExifOrientation, ColorManagementMode.DoNotColorManage);

            var target = Path.Combine(TempDir, $"clipboard-{DateTime.Now:yyyyMMdd-HHmmss}.png");
            var outFile = await StorageFile.GetFileFromPathAsync(await CreateEmptyAsync(target));
            using var output = await outFile.OpenAsync(FileAccessMode.ReadWrite);
            var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, output);
            encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
                decoder.PixelWidth, decoder.PixelHeight, decoder.DpiX, decoder.DpiY, pixels.DetachPixelData());
            await encoder.FlushAsync();
            return target;
        }
        catch
        {
            return null;
        }
    }

    private static Task<string> CreateEmptyAsync(string path)
    {
        using (File.Create(path)) { }
        return Task.FromResult(path);
    }
}
