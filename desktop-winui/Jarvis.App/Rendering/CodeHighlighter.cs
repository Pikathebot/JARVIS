using System.Text.RegularExpressions;
using Microsoft.UI;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Xaml.Media;
using Jarvis_Glass;
using Windows.UI;

namespace Jarvis_App.Rendering;

/// <summary>
/// Lightweight regex-based syntax highlighter, standing in for the old UI's Prism.js
/// (the retired Next.js client's src/components/MessageBubble.tsx registered js/ts/python/bash/json/markdown, with
/// a javascript-grammar fallback). There is no drop-in WinUI 3 equivalent of Prism, and pulling in
/// a full tokenizing library (ColorCode etc.) for six token classes is more machinery than the
/// payoff — this covers comments/strings/numbers/keywords/punctuation, which is what the old
/// syntax colors in globals.css actually distinguished.
/// </summary>
public static class CodeHighlighter
{
    // Token colours: the design system's syntax tokens for the current appearance (read per
    // highlight, so a rebuilt message picks up an appearance change).
    private static Color CommentColor => JarvisPalette.Current.SyntaxComment;
    private static Color StringColor => JarvisPalette.Current.SyntaxString;
    private static Color NumberColor => JarvisPalette.Current.SyntaxNumber;
    private static Color KeywordColor => JarvisPalette.Current.SyntaxKeyword;
    private static Color FunctionColor => JarvisPalette.Current.SyntaxType;
    private static Color PunctuationColor => JarvisPalette.Current.LabelSecondary;
    private static Color PlainColor => JarvisPalette.Current.Label;
    // Declared before KeywordsByLanguage: static field initializers run in textual declaration
    // order, so referencing these from the dictionary literal before their own initializers ran
    // would silently populate every entry with null.
    private static readonly string[] JsKeywords = { "const", "let", "var", "function", "return", "if", "else", "for", "while", "class", "extends", "new", "await", "async", "import", "export", "from", "default", "typeof", "instanceof", "try", "catch", "finally", "throw", "switch", "case", "break", "continue", "null", "undefined", "true", "false", "this", "super", "static" };
    private static readonly string[] TsKeywords = JsKeywords.Concat(new[] { "interface", "type", "enum", "implements", "public", "private", "protected", "readonly", "namespace", "declare", "as", "is", "keyof" }).ToArray();
    private static readonly string[] PyKeywords = { "def", "class", "return", "if", "elif", "else", "for", "while", "import", "from", "as", "try", "except", "finally", "raise", "with", "lambda", "yield", "async", "await", "None", "True", "False", "and", "or", "not", "in", "is", "pass", "break", "continue", "self", "global", "nonlocal" };
    private static readonly string[] ShKeywords = { "if", "then", "else", "fi", "for", "while", "do", "done", "case", "esac", "function", "return", "exit", "echo", "export", "local", "in" };
    private static readonly string[] JsonKeywords = { "true", "false", "null" };
    private static readonly string[] CsKeywords = { "public", "private", "protected", "internal", "static", "class", "interface", "struct", "enum", "namespace", "using", "return", "if", "else", "for", "foreach", "while", "new", "async", "await", "var", "void", "null", "true", "false", "this", "base", "override", "virtual", "abstract", "readonly", "const", "sealed", "partial", "record" };
    private static readonly string[] RustKeywords = { "fn", "let", "mut", "pub", "struct", "enum", "impl", "trait", "use", "mod", "return", "if", "else", "match", "for", "while", "loop", "async", "await", "true", "false", "self", "Self", "static", "const" };

    private static readonly Dictionary<string, string[]> KeywordsByLanguage = new(StringComparer.OrdinalIgnoreCase)
    {
        ["js"] = JsKeywords,
        ["javascript"] = JsKeywords,
        ["jsx"] = JsKeywords,
        ["ts"] = TsKeywords,
        ["typescript"] = TsKeywords,
        ["tsx"] = TsKeywords,
        ["python"] = PyKeywords,
        ["py"] = PyKeywords,
        ["bash"] = ShKeywords,
        ["sh"] = ShKeywords,
        ["shell"] = ShKeywords,
        ["powershell"] = ShKeywords,
        ["json"] = JsonKeywords,
        ["csharp"] = CsKeywords,
        ["cs"] = CsKeywords,
        ["rust"] = RustKeywords,
        ["rs"] = RustKeywords,
    };

    private static readonly Regex CommentRegex = new(@"(//[^\n]*|#[^\n]*|/\*[\s\S]*?\*/)", RegexOptions.Compiled);
    private static readonly Regex StringRegex = new("(\"(?:[^\"\\\\\\n]|\\\\.)*\"|'(?:[^'\\\\\\n]|\\\\.)*'|`(?:[^`\\\\]|\\\\.)*`)", RegexOptions.Compiled);
    private static readonly Regex NumberRegex = new(@"\b\d+(\.\d+)?\b", RegexOptions.Compiled);
    private static readonly Regex PunctuationRegex = new(@"[{}()\[\];,.:=<>+\-*/%!&|^~?]", RegexOptions.Compiled);
    private static readonly Regex WordRegex = new(@"[A-Za-z_][A-Za-z0-9_]*", RegexOptions.Compiled);

    /// <summary>Tokenizes one line of code into colored Inline runs for a RichTextBlock/Paragraph.</summary>
    public static List<Run> HighlightLine(string line, string language)
    {
        var keywords = KeywordsByLanguage.TryGetValue(language, out var kw) ? kw : JsKeywords;
        var keywordSet = new HashSet<string>(keywords, StringComparer.Ordinal);

        var runs = new List<Run>();
        var spans = FindSpans(line, keywordSet);

        var cursor = 0;
        foreach (var span in spans.OrderBy(s => s.Start))
        {
            if (span.Start < cursor) continue; // overlap guard (comments/strings win over everything)
            if (span.Start > cursor)
            {
                runs.Add(PlainRun(line[cursor..span.Start]));
            }
            runs.Add(ColoredRun(line.Substring(span.Start, span.Length), span.Color));
            cursor = span.Start + span.Length;
        }
        if (cursor < line.Length)
        {
            runs.Add(PlainRun(line[cursor..]));
        }
        return runs.Count > 0 ? runs : new List<Run> { PlainRun(line) };
    }

    private readonly record struct Span(int Start, int Length, Color Color);

    private static List<Span> FindSpans(string line, HashSet<string> keywords)
    {
        var spans = new List<Span>();

        // Comments and strings take priority — mask their ranges so nothing inside them is re-tokenized.
        var masked = new bool[line.Length];

        foreach (Match m in CommentRegex.Matches(line))
        {
            spans.Add(new Span(m.Index, m.Length, CommentColor));
            for (var i = m.Index; i < m.Index + m.Length; i++) masked[i] = true;
        }
        foreach (Match m in StringRegex.Matches(line))
        {
            if (masked[m.Index]) continue;
            spans.Add(new Span(m.Index, m.Length, StringColor));
            for (var i = m.Index; i < m.Index + m.Length && i < line.Length; i++) masked[i] = true;
        }
        foreach (Match m in NumberRegex.Matches(line))
        {
            if (masked[m.Index]) continue;
            spans.Add(new Span(m.Index, m.Length, NumberColor));
        }
        foreach (Match m in WordRegex.Matches(line))
        {
            if (masked[m.Index]) continue;
            var word = m.Value;
            if (keywords.Contains(word))
            {
                spans.Add(new Span(m.Index, m.Length, KeywordColor));
            }
            else if (m.Index + m.Length < line.Length && line[m.Index + m.Length] == '(')
            {
                spans.Add(new Span(m.Index, m.Length, FunctionColor));
            }
        }
        foreach (Match m in PunctuationRegex.Matches(line))
        {
            if (masked[m.Index]) continue;
            if (spans.Any(s => s.Start <= m.Index && m.Index < s.Start + s.Length)) continue;
            spans.Add(new Span(m.Index, m.Length, PunctuationColor));
        }

        return spans;
    }

    private static Run PlainRun(string text) => new() { Text = text, Foreground = new SolidColorBrush(PlainColor) };
    private static Run ColoredRun(string text, Color color) => new() { Text = text, Foreground = new SolidColorBrush(color) };
}
