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
/// Timing constants are ported from useVoice.ts: silenceMs 850, maxUtteranceMs 12000,
/// MIN_UTTERANCE_MS 320, BARGE_IN_MS 240. The speech threshold is not: the browser client sat
/// behind getUserMedia's automatic gain control, so its fixed 0.045 RMS was reached by any mic.
/// AudioGraph hands over the raw capture, and a laptop array mic at normal speaking distance
/// peaks around 0.01-0.03 -- the fixed threshold was never crossed and nothing was ever
/// uploaded. Speech is now judged against the measured noise floor (an asymmetric EMA of RMS:
/// falls quickly, rises slowly, and keeps tracking through speech so a loud room can't pin
/// every frame above the threshold and run each utterance to the 12 s cap) with an absolute
/// minimum. The capture is requested mono at the device input node as well as the frame
/// output: on a machine whose render device is 7.1 the graph runs 8 channels, the mono mic was
/// upmixed to 8 and summed back down to 1, and the samples came out 5x over full scale --
/// the WAV clipped hard and Whisper heard "Jarmes". The utterance is peak-normalised before
/// upload so a boosted or quiet mic lands at a level Whisper likes either way.
/// </summary>
public partial class VoiceViewModel : ObservableObject, IDisposable
{
    private const float MinSpeechRms = 0.008f;
    private const float NoiseFloorRatio = 3.0f;
    private const float InitialNoiseFloor = 0.004f;
    private const int SilenceMs = 850;
    private const int MaxUtteranceMs = 12000;
    private const int MinUtteranceMs = 320;
    // Barge-in is judged against our own voice leaking from the speakers into the mic, not the
    // room: while Speaking the floor is allowed to climb fast so the leak *becomes* the floor,
    // and interrupting takes clearly-louder speech held for longer than a word. The browser's
    // 240 ms sat behind echo cancellation this capture path doesn't have.
    private const int BargeInMs = 600;
    private const float BargeInRatio = 1.5f;

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
    private float _noiseFloor = InitialNoiseFloor;

    // One line a second in jarvis-voice.log while the mic is on: frames seen, loudest RMS,
    // current floor/threshold. Enough to tell "no frames" from "too quiet" from "never uploaded".
    private int _statFrames;
    private float _statPeakRms;
    private double _statAccumMs;

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

            _sampleRate = (int)_graph.EncodingProperties.SampleRate;
            // The graph's own encoding follows the render device (2, or 8 on a surround setup).
            // Ask for mono at both ends so one sample is one instant and nothing gets summed.
            var mono = AudioEncodingProperties.CreatePcm((uint)_sampleRate, 1, 32);
            mono.Subtype = MediaEncodingSubtypes.Float;

            var inputResult = await _graph.CreateDeviceInputNodeAsync(MediaCategory.Speech, mono);
            if (inputResult.Status == AudioDeviceNodeCreationStatus.FormatNotSupported)
            {
                // Some capture drivers refuse a mono request; fall back to the graph format and
                // let the frame output node do the downmix (NormalizeGain handles the level).
                App.LogVoice("mic: mono capture not supported, using the graph format");
                inputResult = await _graph.CreateDeviceInputNodeAsync(MediaCategory.Speech);
            }
            if (inputResult.Status != AudioDeviceNodeCreationStatus.Success)
            {
                IsSupported = false;
                ErrorMessage = $"Could not open microphone: {inputResult.Status}";
                _graph.Dispose();
                _graph = null;
                return;
            }

            _frameOutputNode = _graph.CreateFrameOutputNode(mono);
            inputResult.DeviceInputNode.AddOutgoingConnection(_frameOutputNode);
            _graph.QuantumProcessed += Graph_QuantumProcessed;

            _noiseFloor = InitialNoiseFloor;
            _statFrames = 0;
            _statPeakRms = 0;
            _statAccumMs = 0;
            _graph.Start();
            IsActive = true;
            IsSupported = true;
            ErrorMessage = null;
            State = VoiceState.Listening;
            var g = _graph.EncodingProperties;
            App.LogVoice($"mic on: graph {g.SampleRate} Hz x{g.ChannelCount} {g.Subtype}, frames mono {mono.SampleRate} Hz, " +
                         $"device '{inputResult.DeviceInputNode.Device?.Name}', quantum {_graph.SamplesPerQuantum} samples");
        }
        catch (Exception ex)
        {
            IsSupported = false;
            ErrorMessage = ex.Message;
            App.LogVoice($"mic start failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    public void Stop()
    {
        if (_graph is not null)
        {
            _graph.QuantumProcessed -= Graph_QuantumProcessed;
            _graph.Stop();
            _graph.Dispose();
            _graph = null;
        }
        _frameOutputNode = null;
        if (IsActive) App.LogVoice("mic off");
        IsActive = false;
        State = VoiceState.Idle;
        Level = 0;
        _captureBuffer.Clear();
        _isRecording = false;
    }

    private int _quanta, _emptyFrames, _readFailures;

    private void Graph_QuantumProcessed(AudioGraph sender, object args)
    {
        if (_frameOutputNode is null) return;

        _quanta++;
        using var frame = _frameOutputNode.GetFrame();

        float[] samples;
        try
        {
            samples = AudioFrameReader.ReadSamples(frame);
        }
        catch (Exception ex)
        {
            if (_readFailures++ == 0) App.LogVoice($"frame read failed: {ex.GetType().Name}: {ex.Message}");
            LogQuantumHealth();
            return;
        }
        if (samples.Length == 0)
        {
            _emptyFrames++;
            LogQuantumHealth();
            return;
        }

        var sumSquares = 0.0;
        foreach (var s in samples) sumSquares += (double)s * s;
        var rms = (float)Math.Sqrt(sumSquares / samples.Length);

        var quantumMs = samples.Length / (double)_sampleRate * 1000.0;
        var threshold = Math.Max(MinSpeechRms, _noiseFloor * NoiseFloorRatio);
        var speaking = rms >= threshold;
        // Track the room: drop to a quieter level almost at once, climb slowly (~5 s to reach a
        // sustained new level at 100 frames/s). Climbing also while "speaking" is what lets the
        // floor recover from a wrong start -- with an update-only-when-quiet rule, a floor that
        // begins below the room's real noise never sees a quiet frame and never moves.
        var riseRate = State == VoiceState.Speaking ? 0.05f : (speaking ? 0.002f : 0.02f);
        _noiseFloor += (rms - _noiseFloor) * (rms < _noiseFloor ? 0.2f : riseRate);

        _statFrames++;
        _statPeakRms = Math.Max(_statPeakRms, rms);
        _statAccumMs += quantumMs;
        if (_statAccumMs >= 1000)
        {
            App.LogVoice($"{_statFrames} frames/s ({_quanta} quanta, {_emptyFrames} empty, {_readFailures} unreadable), " +
                         $"peak rms {_statPeakRms:F4}, floor {_noiseFloor:F4}, threshold {threshold:F4}, " +
                         $"state {State}{(_isRecording ? $", recording {_recordingMs:F0} ms" : "")}");
            _statFrames = 0;
            _statPeakRms = 0;
            _statAccumMs = 0;
            _quanta = 0;
            _emptyFrames = 0;
            _readFailures = 0;
        }

        _dispatcher.TryEnqueue(() => Level = Math.Min(1.0, rms / Math.Max(threshold * 4, 0.02f)));

        if (State == VoiceState.Speaking)
        {
            // Barge-in watch: sustained speech while our own TTS is playing cuts it off.
            if (rms >= threshold * BargeInRatio)
            {
                _bargeInAccumMs += quantumMs;
                if (_bargeInAccumMs >= BargeInMs)
                {
                    App.LogVoice($"barge-in: rms {rms:F4} over {threshold * BargeInRatio:F4} for {_bargeInAccumMs:F0} ms, cutting playback");
                    _bargeInAccumMs = 0;
                    // Leave Speaking here, not just in CancelPlayback: the frames that follow
                    // must reach the silence detector below or the recording never ends and
                    // the UI sits on "speaking" with nothing playing.
                    State = VoiceState.Listening;
                    _dispatcher.TryEnqueue(CancelPlayback);
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

    /// <summary>Every ~100 quanta (1 s) with no usable frame, say so -- otherwise a dead
    /// graph looks identical to a silent room in the log.</summary>
    private void LogQuantumHealth()
    {
        if (_quanta % 100 != 0) return;
        App.LogVoice($"no audio: {_quanta} quanta, {_emptyFrames} empty frames, {_readFailures} unreadable");
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

        App.LogVoice($"utterance captured: {snapshot.Length / (double)_sampleRate * 1000:F0} ms, gain x{NormalizeGain(snapshot):F2}, uploading");
        _dispatcher.TryEnqueue(() => State = VoiceState.Thinking);
        _ = UploadUtteranceAsync(snapshot);
    }

    /// <summary>Gain that brings the utterance's peak to 0.9 full scale: clipped input (a mic
    /// boost, a summed upmix) is pulled back under 1.0, and a quiet mic is lifted by up to 10x.
    /// Whisper is tolerant of level but not of a square wave.</summary>
    private static float NormalizeGain(float[] samples)
    {
        var peak = 0f;
        foreach (var s in samples) peak = Math.Max(peak, Math.Abs(s));
        if (peak <= 1e-4f) return 1f;
        return Math.Clamp(0.9f / peak, 0.05f, 10f);
    }

    private async Task UploadUtteranceAsync(float[] samples)
    {
        try
        {
            var gain = NormalizeGain(samples);
            if (Math.Abs(gain - 1f) > 0.01f)
            {
                for (var i = 0; i < samples.Length; i++) samples[i] *= gain;
            }
            var wav = WavEncoder.EncodeMono16Bit(samples, _sampleRate);
            using var stream = new MemoryStream(wav);
            Services.ForegroundGrant.ToBackend();
            var result = await _api.ListenChunkAsync(stream, _sessionId).ConfigureAwait(false);
            App.LogVoice($"listen result: state {result.Session.State}, respond {result.ShouldRespond}, " +
                         $"transcript '{result.Transcript}', query '{result.Query}'");

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
            App.LogVoice($"listen upload failed: {ex.GetType().Name}: {ex.Message}");
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
            App.LogVoice($"say: {result.AudioBase64?.Length ?? 0} base64 chars for '{Truncate(result.SpokenText, 60)}'");
            if (string.IsNullOrEmpty(result.AudioBase64))
            {
                // Backend had no audio (edge-tts offline, empty text after sanitising): nothing
                // to play, so don't sit in Speaking waiting for a MediaEnded that never comes.
                _dispatcher.TryEnqueue(() =>
                {
                    SpokenText = result.SpokenText;
                    ErrorMessage = "No speech audio came back from the backend.";
                    ResumeAfterPlayback();
                });
                return;
            }
            _dispatcher.TryEnqueue(() =>
            {
                SpokenText = result.SpokenText;
                State = VoiceState.Speaking;
                PlayAudio(result.AudioBase64);
            });
        }
        catch (Exception ex)
        {
            App.LogVoice($"say failed: {ex.GetType().Name}: {ex.Message}");
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
            _player.MediaFailed += Player_MediaFailed;
            _player.MediaOpened += Player_MediaOpened;
            _player.Source = MediaSource.CreateFromUri(new Uri(_tempAudioPath));
            _player.Play();
            App.LogVoice($"play: {bytes.Length} bytes, volume {_player.Volume:F2}, muted {_player.IsMuted}, device '{_player.AudioDevice?.Name ?? "(default)"}'");
        }
        catch (Exception ex)
        {
            App.LogVoice($"play failed: {ex.GetType().Name}: {ex.Message}");
            ErrorMessage = ex.Message;
            State = VoiceState.Listening;
        }
    }

    private void Player_MediaOpened(MediaPlayer sender, object args)
    {
        App.LogVoice($"play opened: {sender.PlaybackSession.NaturalDuration.TotalMilliseconds:F0} ms");
    }

    private void Player_MediaFailed(MediaPlayer sender, MediaPlayerFailedEventArgs args)
    {
        // Without this the state sat in Speaking forever when the MP3 couldn't be decoded or the
        // render device refused it -- "Jarvis is speaking" and no sound, no recovery.
        App.LogVoice($"play failed: {args.Error} 0x{args.ExtendedErrorCode.HResult:X8} {args.ErrorMessage}");
        _dispatcher.TryEnqueue(() =>
        {
            ErrorMessage = $"Speech playback failed: {args.Error}";
            ResumeAfterPlayback();
        });
    }

    private void Player_MediaEnded(MediaPlayer sender, object args)
    {
        App.LogVoice("play ended");
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

    /// <summary>Back to whatever the mic state should be when nothing is playing.</summary>
    private void ResumeAfterPlayback()
    {
        CancelPlayback();
        State = IsActive ? VoiceState.Listening : VoiceState.Idle;
    }

    private static string Truncate(string? text, int max) =>
        string.IsNullOrEmpty(text) ? "" : text.Length <= max ? text : text[..max] + "...";

    private void CancelPlayback()
    {
        if (_player is not null)
        {
            _player.MediaEnded -= Player_MediaEnded;
            _player.MediaFailed -= Player_MediaFailed;
            _player.MediaOpened -= Player_MediaOpened;
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
