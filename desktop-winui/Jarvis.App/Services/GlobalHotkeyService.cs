using System.Runtime.InteropServices;

namespace Jarvis_App.Services;

/// <summary>
/// Global hotkey with a fallback chain, replacing the Rust implementation in
/// the retired Tauri shell's main.rs (RegisterHotKey candidates tried in order; the first that
/// registers wins, and the winner is reported back so the UI shows the real binding instead of a
/// guessed one). RegisterHotKey needs a window handle owned by a thread with a message loop; we
/// piggyback on the main window's HWND via SetWindowSubclass (comctl32) to intercept WM_HOTKEY
/// without replacing WinUI's own WndProc.
/// </summary>
public sealed class GlobalHotkeyService : IDisposable
{
    private static readonly (uint Modifiers, uint VirtualKey, string Label)[] Candidates =
    {
        (ModControl | ModShift, VkJ, "Ctrl+Shift+J"),
        (ModControl | ModAlt, VkJ, "Ctrl+Alt+J"),
        (ModControl | ModShift, VkOem3 /* ` */, "Ctrl+Shift+`"),
        (ModAlt | ModShift, VkJ, "Alt+Shift+J"),
    };

    private const uint ModAlt = 0x0001;
    private const uint ModControl = 0x0002;
    private const uint ModShift = 0x0004;
    private const uint ModNoRepeat = 0x4000;
    private const uint VkJ = 0x4A;
    private const uint VkOem3 = 0xC0;
    private const int WmHotkey = 0x0312;
    private const int HotkeyId = 1;

    private nint _hwnd;
    private SubclassProc? _subclassDelegate;
    private bool _registered;

    public string? RegisteredHotkeyLabel { get; private set; }

    public event Action? HotkeyPressed;

    public void Register(nint hwnd)
    {
        _hwnd = hwnd;
        _subclassDelegate = WndProc;
        SetWindowSubclass(hwnd, _subclassDelegate, HotkeyId, nint.Zero);

        foreach (var (modifiers, vk, label) in Candidates)
        {
            if (RegisterHotKey(hwnd, HotkeyId, modifiers | ModNoRepeat, vk))
            {
                RegisteredHotkeyLabel = label;
                _registered = true;
                return;
            }
        }

        // Every candidate was already claimed by another application — the HUD stays reachable
        // from the main window's toggle button, same fallback story as the Tauri version.
        RegisteredHotkeyLabel = null;
    }

    private nint WndProc(nint hWnd, uint msg, nint wParam, nint lParam, nint uIdSubclass, nint dwRefData)
    {
        if (msg == WmHotkey && wParam.ToInt32() == HotkeyId)
        {
            HotkeyPressed?.Invoke();
        }
        return DefSubclassProc(hWnd, msg, wParam, lParam);
    }

    public void Dispose()
    {
        if (_registered && _hwnd != nint.Zero)
        {
            UnregisterHotKey(_hwnd, HotkeyId);
        }
        if (_hwnd != nint.Zero && _subclassDelegate is not null)
        {
            RemoveWindowSubclass(_hwnd, _subclassDelegate, HotkeyId);
        }
    }

    private delegate nint SubclassProc(nint hWnd, uint msg, nint wParam, nint lParam, nint uIdSubclass, nint dwRefData);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RegisterHotKey(nint hWnd, int id, uint fsModifiers, uint vk);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnregisterHotKey(nint hWnd, int id);

    [DllImport("comctl32.dll", SetLastError = true)]
    private static extern bool SetWindowSubclass(nint hWnd, SubclassProc pfnSubclass, nint uIdSubclass, nint dwRefData);

    [DllImport("comctl32.dll", SetLastError = true)]
    private static extern bool RemoveWindowSubclass(nint hWnd, SubclassProc pfnSubclass, nint uIdSubclass);

    [DllImport("comctl32.dll")]
    private static extern nint DefSubclassProc(nint hWnd, uint msg, nint wParam, nint lParam);
}
