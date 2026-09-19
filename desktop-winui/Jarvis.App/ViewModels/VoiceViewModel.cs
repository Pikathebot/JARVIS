using CommunityToolkit.Mvvm.ComponentModel;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis_App.Services;
using Microsoft.UI.Dispatching;
using Windows.Media;
using Windows.Media.Audio;
using Windows.Media.Capture;
using Windows.Media.Core;
using Windows.Media.MediaProperties;
using Windows.Media.Playback;
using Windows.Media.Render;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Native port of the retired Next.js client's src/hooks/useVoice.ts. The client owns the microphone and does
/// local voice-activity detection (RMS over AudioGraph frames); the backend owns what an
/// utterance means (wake-word detection, arming, transcription) — VoiceListenResult.Session.State
/// is authoritative and this view model mirrors it rather than running its own arm-window timer.
///
/// Constants are ported exactly from useVoice.ts so behavior matches the TS client:
/// speechThreshold 0.045 (RMS), silenceMs 850, maxUtteranceMs 12000, MIN_UTTERANCE_MS 320,
/// BARGE_IN_MS 240. LEVEL_POLL_MS doesn't apply here — AudioGraph delivers frames on its own
/// quantum (~10ms), so Level updates every frame rather than being throttled.
/// </summary>
public partial class VoiceViewModel : ObservableObject, IDisposable
{
    private const float SpeechThreshold = 0.045f;
    private const int SilenceMs = 850;
    private const int MaxUtteranceMs = 12000;
    private const int MinUtteranceMs = 320;
    private const int BargeInMs = 240;

    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;
    private readonly string _sessionId;

    private AudioGraph? _graph;
    private AudioFrameOutputNode? _frameOutputNode;
    private MediaPlayer? _player;
    private string? _tempAudioPath;

    private readonly List<float> _captureBuffer = new();
    private bool _isRecording;
    private double _silenceAccumMs;
    private double _recordingMs;
    private double _bargeInAccumMs;
    private int _sampleRate = 48000;

    [ObservableProperty]
    public partial bool IsSupported { get; set; } = true;

    [ObservableProperty]
    public partial bool IsActive { get; set; }

    [ObservableProperty]
    public partial VoiceState State { get; set; } = VoiceState.Idle;

    [ObservableProperty]
    public partial double Level { get; set; }

    [ObservableProperty]
    public partial string Transcript { get; set; } = "";

    [ObservableProperty]
    public partial string SpokenText { get; set; } = "";

    [ObservableProperty]
    public partial string? ErrorMessage { get; set; }

    /// <summary>Fires with the query text when the backend decides an utterance should be answered.</summary>
    public event Action<string>? CommandReceived;

    public VoiceViewModel(JarvisApiClient api, DispatcherQueue dispatcher, string sessionId)
    {
        _api = api;
        _dispatcher = dispatcher;
        _sessionId = sessionId;
    }

    public async Task StartAsync()
    {
        if (IsActive) return;

        try
        {
            var settings = new AudioGraphSettings(AudioRenderCategory.Speech);
            var graphResult = await AudioGraph.CreateAsync(settings);
            if (graphResult.Status != AudioGraphCreationStatus.Success)
            {
                IsSupported = false;
                ErrorMessage = $"Could not create audio graph: {graphResult.Status}";
                return;
            }
            _graph = graphResult.Graph;

            var inputResult = await _graph.CreateDeviceInputNodeAsync(MediaCategory.Speech);
            if (inputResult.Status != AudioDeviceNodeCreationStatus.Success)
            {
                IsSupported = false;
                ErrorMessage = $"Could not open microphone: {inputResult.Status}";
                _graph.Dispose();
                _graph = null;
                return;
            }

            _sampleRate = (int)_graph.EncodingProperties.SampleRate;
            _frameOutputNode = _graph.CreateFrameOutputNode();
            inputResult.DeviceInputNode.AddOutgoingConnection(_frameOutputNode);
            _graph.QuantumStarted += Graph_QuantumStarted;

            _graph.Start();
            IsActive = true;
            IsSupported = true;
            ErrorMessage = null;
            State = VoiceState.Listening;
        }
        catch (Exception ex)
        {
            IsSupported = false;
            ErrorMessage = ex.Message;
        }
    }

    public void Stop()
    {
        if (_graph is not null)
        {
            _graph.QuantumStarted -= Graph_QuantumStarted;
            _graph.Stop();
            _graph.Dispose();
            _graph = null;
        }
        _frameOutputNode = null;
        IsActive = false;
        State = VoiceState.Idle;
        Level = 0;
        _captureBuffer.Clear();
        _isRecording = false;
    }

    private void Graph_QuantumStarted(AudioGraph sender, object args)
    {
        if (_frameOutputNode is null) return;

        using var frame = _frameOutputNode.GetFrame();
        if (frame.Duration is null) return;

        float[] samples;
        try
        {
            samples = AudioFrameReader.ReadSamples(frame);
        }
        catch
        {
            return; // frame not ready / dropped — skip this quantum
        }
        if (samples.Length == 0) return;

        var sumSquares = 0.0;
        foreach (var s in samples) sumSquares += (double)s * s;
        var rms = (float)Math.Sqrt(sumSquares / samples.Length);

        var quantumMs = samples.Length / (double)_sampleRate * 1000.0;
        var speaking = rms >= SpeechThreshold;

        _dispatcher.TryEnqueue(() => Level = Math.Min(1.0, rms * 8));

        if (State == VoiceState.Speaking)
        {
            // Barge-in watch: sustained speech while our own TTS is playing cuts it off.
            if (speaking)
            {
                _bargeInAccumMs += quantumMs;
                if (_bargeInAccumMs >= BargeInMs)
                {
                    _dispatcher.TryEnqueue(CancelPlayback);
                    _bargeInAccumMs = 0;
                    BeginRecording();
                    AppendSamples(samples);
                    _recordingMs += quantumMs;
                }
            }
            else
            {
                _bargeInAccumMs = 0;
            }
            return;
        }

        if (speaking)
        {
            _silenceAccumMs = 0;
            if (!_isRecording)
            {
                BeginRecording();
            }
            AppendSamples(samples);
            _recordingMs += quantumMs;

            if (_recordingMs >= MaxUtteranceMs)
            {
                FinishRecordingAndUpload();
            }
        }
        else if (_isRecording)
        {
            AppendSamples(samples);
            _recordingMs += quantumMs;
            _silenceAccumMs += quantumMs;

            if (_silenceAccumMs >= SilenceMs)
            {
                if (_recordingMs - _silenceAccumMs >= MinUtteranceMs)
                {
                    FinishRecordingAndUpload();
                }
                else
                {
                    // Too short to be real speech (a cough, a click) — discard.
                    _captureBuffer.Clear();
                    _isRecording = false;
                    _recordingMs = 0;
                    _silenceAccumMs = 0;
                }
            }
        }
    }

    private void BeginRecording()
    {
        _isRecording = true;
        _captureBuffer.Clear();
        _recordingMs = 0;
        _silenceAccumMs = 0;
    }

    private void AppendSamples(float[] samples)
    {
        lock (_captureBuffer)
        {
            _captureBuffer.AddRange(samples);
        }
    }

    private void FinishRecordingAndUpload()
    {
        _isRecording = false;
        float[] snapshot;
        lock (_captureBuffer)
        {
            snapshot = _captureBuffer.ToArray();
            _captureBuffer.Clear();
        }
        _recordingMs = 0;
        _silenceAccumMs = 0;

        if (snapshot.Length == 0) return;

        _dispatcher.TryEnqueue(() => State = VoiceState.Thinking);
        _ = UploadUtteranceAsync(snapshot);
    }

    private async Task UploadUtteranceAsync(float[] samples)
    {
        try
        {
            var wav = WavEncoder.EncodeMono16Bit(samples, _sampleRate);
            using var stream = new MemoryStream(wav);
            var result = await _api.ListenChunkAsync(stream, _sessionId).ConfigureAwait(false);

            _dispatcher.TryEnqueue(() =>
            {
                Transcript = result.Transcript;
                State = result.Session.State;

                if (result.ShouldRespond && !string.IsNullOrWhiteSpace(result.Query))
                {
                    CommandReceived?.Invoke(result.Query);
                }
                else if (State == VoiceState.Thinking)
                {
                    // Not a real command (e.g. bare wake word only) — nothing else will move us
                    // off Thinking, so fall back to whatever the server actually reported.
                    State = result.Session.State;
                }
            });
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() =>
            {
                ErrorMessage = ex.Message;
                State = VoiceState.Listening;
            });
        }
    }

    /// <summary>Synthesizes and plays text via /api/voice/say — called by the page-level
    /// orchestrator (MainWindow/HudWindow) once a chat reply is ready, matching useVoice.ts's
    /// externally-driven speak() call rather than VoiceViewModel initiating chat itself.</summary>
    public async Task SpeakAsync(string text, string? voiceId = null)
    {
        try
        {
            var result = await _api.SayAsync(text, _sessionId, voiceId).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                SpokenText = result.SpokenText;
                State = VoiceState.Speaking;
                PlayAudio(result.AudioBase64);
            });
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() => ErrorMessage = ex.Message);
        }
    }

    private void PlayAudio(string audioBase64)
    {
        CancelPlayback();

        try
        {
            var bytes = Convert.FromBase64String(audioBase64);
            _tempAudioPath = Path.Combine(Path.GetTempPath(), $"jarvis-tts-{Guid.NewGuid():N}.mp3");
            File.WriteAllBytes(_tempAudioPath, bytes);

            _player = new MediaPlayer();
            _player.MediaEnded += Player_MediaEnded;
            _player.Source = MediaSource.CreateFromUri(new Uri(_tempAudioPath));
            _player.Play();
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            State = VoiceState.Listening;
        }
    }

    private void Player_MediaEnded(MediaPlayer sender, object args)
    {
        _dispatcher.TryEnqueue(async () =>
        {
            State = VoiceState.Armed;
            try
            {
                await _api.ArmFollowUpAsync(_sessionId).ConfigureAwait(false);
            }
            catch
            {
                // best-effort
            }
        });
    }

    private void CancelPlayback()
    {
        if (_player is not null)
        {
            _player.MediaEnded -= Player_MediaEnded;
            _player.Pause();
            _player.Source = null;
            _player.Dispose();
            _player = null;
        }
        if (_tempAudioPath is not null && File.Exists(_tempAudioPath))
        {
            try { File.Delete(_tempAudioPath); } catch { /* best-effort cleanup */ }
            _tempAudioPath = null;
        }
    }

    public void Dispose()
    {
        Stop();
        CancelPlayback();
    }
}
