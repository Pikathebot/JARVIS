using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

public sealed class PersonaProfile
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";

    [JsonPropertyName("address_term")]
    public string AddressTerm { get; set; } = "";

    [JsonPropertyName("voice_id")]
    public string VoiceId { get; set; } = "";

    [JsonPropertyName("tone_directives")]
    public List<string> ToneDirectives { get; set; } = new();

    public List<string> Acknowledgements { get; set; } = new();
    public string Greeting { get; set; } = "";

    [JsonPropertyName("max_speech_sentences")]
    public int MaxSpeechSentences { get; set; }
}

public sealed class PersonaSummary
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";

    /// <summary>The id, capitalised ("operator" -> "Operator"): every built-in persona is
    /// *named* Jarvis, so the name alone can't tell them apart in a list.</summary>
    [System.Text.Json.Serialization.JsonIgnore]
    public string DisplayName => string.IsNullOrEmpty(Id) ? Name : char.ToUpperInvariant(Id[0]) + Id[1..];

    public string Description { get; set; } = "";
}

public sealed class PersonaOverrides
{
    [JsonPropertyName("address_term")]
    public string? AddressTerm { get; set; }

    [JsonPropertyName("voice_id")]
    public string? VoiceId { get; set; }

    public string? Greeting { get; set; }

    [JsonPropertyName("max_speech_sentences")]
    public int? MaxSpeechSentences { get; set; }
}

public sealed class PersonaStatus
{
    [JsonPropertyName("active_id")]
    public string ActiveId { get; set; } = "";

    public PersonaProfile Active { get; set; } = new();
    public PersonaOverrides Overrides { get; set; } = new();
    public List<PersonaSummary> Available { get; set; } = new();

    [JsonPropertyName("available_voices")]
    public Dictionary<string, string> AvailableVoices { get; set; } = new();
}
