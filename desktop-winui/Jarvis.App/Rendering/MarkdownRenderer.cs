using System.Text;
using System.Text.RegularExpressions;
using Markdig;
using Markdig.Extensions.Tables;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;
using Microsoft.UI;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace Jarvis_App.Rendering;

/// <summary>
/// Native replacement for MessageBubble.tsx's SafeMarkdown (react-markdown + remark-gfm) and
/// CodeBlock (Prism). Produces a plain WinUI element tree — no WebView2 — so it composites with
/// the glass material and animates like any other XAML content. Covers: paragraphs, headings,
/// bold/italic/inline-code, fenced code blocks (via CodeHighlighter, with a language label + Copy
/// button), bullet/ordered lists, block quotes, GFM tables, thematic breaks, and links. Also
/// splits a leading/embedded &lt;think&gt;...&lt;/think&gt; block into a collapsible "Thought
/// Process" Expander, matching SafeMarkdown's behavior (auto-open while streaming with no answer
/// text yet).
/// </summary>
public static class MarkdownRenderer
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder()
        .UseAdvancedExtensions() // includes GFM tables, task lists, autolinks, strikethrough
        .Build();

    private static readonly Regex ThinkBlockRegex = new(@"<think>([\s\S]*?)(</think>|$)", RegexOptions.Compiled);

    /// <summary>
    /// The cheap counterpart to <see cref="Render"/>, for use while tokens are still arriving.
    ///
    /// Render builds one element per markdown construct -- a list item alone costs two
    /// StackPanels, a TextBlock and a brush, and every Children.Add crosses into WinRT through
    /// reflection -- then WinUI lays the whole tree out again. Rebuilding that on a partial
    /// message is what pinned the UI thread until Windows killed the window as hung. A streaming
    /// answer does not need real formatting; it needs to arrive. So while streaming we produce
    /// plain text for a single reused TextBlock, and pay for the real tree once, at the end.
    ///
    /// The &lt;think&gt; block is stripped rather than shown: it is the model's scratchpad, and
    /// during streaming it is usually the only thing there is.
    /// </summary>
    /// <param name="hasStructuredReasoning">True when this message's reasoning is routed through
    /// the structured "reasoning" SSE channel (see ChatMessage.HasStructuredReasoning) and rendered
    /// by the Reasoning tab instead -- in that case Content never contains &lt;think&gt; tags to
    /// begin with, so the scan is skipped rather than risk showing the same text twice.</param>
    public static string StreamingText(string content, bool hasStructuredReasoning = false)
    {
        if (hasStructuredReasoning) return content;

        var (thinking, complete, remainder) = ExtractThink(content);
        if (thinking is null) return content;
        // Before any answer text exists, showing nothing would look like a stall, so surface the
        // thinking itself until the model starts answering.
        return remainder.Length > 0 ? remainder
             : complete ? ""
             : thinking.TrimStart();
    }

    /// <param name="hasStructuredReasoning">See <see cref="StreamingText"/> -- when true, the
    /// inline "Thought Process" Expander is skipped entirely and Content is parsed as-is, since the
    /// Reasoning tab already owns this message's thinking text.</param>
    public static Panel Render(string content, bool isStreaming, bool hasStructuredReasoning = false)
    {
        var root = new StackPanel { Spacing = 6 };
        var remainder = content;

        if (!hasStructuredReasoning)
        {
            var (thinking, thinkingComplete, rest) = ExtractThink(content);
            if (thinking is not null)
            {
                root.Children.Add(BuildThinkExpander(thinking, thinkingComplete, isStreaming, rest.Length == 0));
            }
            remainder = rest;
        }

        if (remainder.Length > 0)
        {
            var document = Markdown.Parse(remainder, Pipeline);
            foreach (var block in document)
            {
                var element = RenderBlock(block);
                if (element is not null) root.Children.Add(element);
            }
        }

        return root;
    }

    private static (string? Thinking, bool Complete, string Remainder) ExtractThink(string content)
    {
        var match = ThinkBlockRegex.Match(content);
        if (!match.Success)
        {
            return (null, false, content);
        }

        var complete = match.Groups[2].Value == "</think>";
        var thinking = match.Groups[1].Value;
        var remainder = content.Remove(match.Index, match.Length);
        return (thinking, complete, remainder.TrimStart());
    }

    private static Expander BuildThinkExpander(string thinking, bool complete, bool isStreaming, bool noAnswerYet)
    {
        var expander = new Expander
        {
            Header = complete ? "Thought Process" : "Thinking…",
            IsExpanded = isStreaming && noAnswerYet,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
        };
        expander.Content = new TextBlock
        {
            Text = thinking.Trim(),
            TextWrapping = TextWrapping.Wrap,
            Foreground = new SolidColorBrush(Color.FromArgb(255, 148, 163, 184)),
            FontSize = 13,
        };
        return expander;
    }

    private static FrameworkElement? RenderBlock(Markdig.Syntax.Block block)
    {
        switch (block)
        {
            case HeadingBlock heading:
                return RenderHeading(heading);
            case ParagraphBlock paragraph:
                return RenderParagraphBlock(paragraph);
            case Markdig.Syntax.FencedCodeBlock fenced:
                return RenderCodeBlock(fenced);
            case Markdig.Syntax.CodeBlock code:
                return RenderCodeBlock(code, language: "");
            case ListBlock list:
                return RenderList(list);
            case QuoteBlock quote:
                return RenderQuote(quote);
            case ThematicBreakBlock:
                return new Border { Height = 1, Background = new SolidColorBrush(Color.FromArgb(30, 255, 255, 255)), Margin = new Thickness(0, 8, 0, 8) };
            case Table table:
                return RenderTable(table);
            default:
                if (block is LeafBlock leaf && leaf.Inline is not null)
                {
                    return TextBlockFromInlines(leaf.Inline, 14);
                }
                return null;
        }
    }

    private static TextBlock RenderHeading(HeadingBlock heading)
    {
        var tb = heading.Inline is not null ? TextBlockFromInlines(heading.Inline, HeadingFontSize(heading.Level)) : new TextBlock();
        tb.FontWeight = FontWeights.Bold;
        tb.Margin = new Thickness(0, heading.Level <= 2 ? 8 : 4, 0, 4);
        return tb;
    }

    private static double HeadingFontSize(int level) => level switch
    {
        1 => 22,
        2 => 19,
        3 => 17,
        4 => 15,
        _ => 14,
    };

    private static TextBlock? RenderParagraphBlock(ParagraphBlock paragraph) =>
        paragraph.Inline is null ? null : TextBlockFromInlines(paragraph.Inline, 14);

    private static TextBlock TextBlockFromInlines(ContainerInline inlines, double fontSize)
    {
        var tb = new TextBlock
        {
            TextWrapping = TextWrapping.Wrap,
            FontSize = fontSize,
            Foreground = new SolidColorBrush(Color.FromArgb(255, 243, 247, 252)),
        };
        AppendInlines(tb.Inlines, inlines);
        return tb;
    }

    private static void AppendInlines(InlineCollection target, ContainerInline container)
    {
        foreach (var inline in container)
        {
            switch (inline)
            {
                case LiteralInline literal:
                    target.Add(new Run { Text = literal.Content.ToString() });
                    break;
                case EmphasisInline emphasis:
                    if (emphasis.DelimiterCount == 2)
                    {
                        var bold = new Bold();
                        AppendInlines(bold.Inlines, emphasis);
                        target.Add(bold);
                    }
                    else
                    {
                        var italic = new Italic();
                        AppendInlines(italic.Inlines, emphasis);
                        target.Add(italic);
                    }
                    break;
                case CodeInline code:
                    target.Add(new Run
                    {
                        Text = code.Content,
                        FontFamily = new FontFamily("Cascadia Mono, Consolas, monospace"),
                        Foreground = new SolidColorBrush(Color.FromArgb(255, 103, 232, 249)),
                    });
                    break;
                case LinkInline link:
                    var hyperlink = new Hyperlink();
                    if (Uri.TryCreate(link.Url, UriKind.Absolute, out var uri))
                    {
                        hyperlink.NavigateUri = uri;
                    }
                    AppendInlines(hyperlink.Inlines, link);
                    target.Add(hyperlink);
                    break;
                case LineBreakInline:
                    target.Add(new LineBreak());
                    break;
                case ContainerInline container2:
                    AppendInlines(target, container2);
                    break;
                default:
                    if (inline is LeafInline leafInline)
                    {
                        target.Add(new Run { Text = leafInline.ToString() });
                    }
                    break;
            }
        }
    }

    private static FrameworkElement RenderCodeBlock(Markdig.Syntax.CodeBlock code, string? language = null)
    {
        var lang = language ?? (code as Markdig.Syntax.FencedCodeBlock)?.Info ?? "";
        var text = string.Join('\n', code.Lines.Lines.Take(code.Lines.Count).Select(l => l.ToString()));

        var container = new Border
        {
            Background = new SolidColorBrush(Color.FromArgb(200, 5, 7, 11)),
            CornerRadius = new CornerRadius(10),
            BorderThickness = new Thickness(1),
            BorderBrush = new SolidColorBrush(Color.FromArgb(30, 255, 255, 255)),
            Margin = new Thickness(0, 4, 0, 4),
        };

        var grid = new Grid();
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        var header = new Grid { Padding = new Thickness(12, 6, 8, 6) };
        header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        header.Children.Add(new TextBlock
        {
            Text = string.IsNullOrWhiteSpace(lang) ? "text" : lang,
            Foreground = new SolidColorBrush(Color.FromArgb(255, 100, 116, 139)),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Center,
        });

        var copyButton = new Button { Content = "Copy", FontSize = 11, Padding = new Thickness(8, 2, 8, 2) };
        Grid.SetColumn(copyButton, 1);
        copyButton.Click += (_, _) =>
        {
            var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
            package.SetText(text);
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
            copyButton.Content = "Copied";
            var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
            timer.Tick += (_, _) => { copyButton.Content = "Copy"; timer.Stop(); };
            timer.Start();
        };
        header.Children.Add(copyButton);
        Grid.SetRow(header, 0);
        grid.Children.Add(header);

        var codeText = new TextBlock
        {
            FontFamily = new FontFamily("Cascadia Mono, Consolas, monospace"),
            FontSize = 12.5,
            TextWrapping = TextWrapping.NoWrap,
            Padding = new Thickness(12, 4, 12, 12),
            IsTextSelectionEnabled = true,
        };
        var lines = text.Split('\n');
        for (var i = 0; i < lines.Length; i++)
        {
            foreach (var run in CodeHighlighter.HighlightLine(lines[i], lang))
            {
                codeText.Inlines.Add(run);
            }
            if (i < lines.Length - 1) codeText.Inlines.Add(new LineBreak());
        }

        var scroller = new ScrollViewer
        {
            HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollMode = ScrollMode.Disabled,
            Content = codeText,
        };
        Grid.SetRow(scroller, 1);
        grid.Children.Add(scroller);

        container.Child = grid;
        return container;
    }

    private static FrameworkElement RenderList(ListBlock list)
    {
        var panel = new StackPanel { Spacing = 2, Margin = new Thickness(4, 2, 0, 2) };
        var index = list.OrderedStart is not null ? int.Parse(list.OrderedStart) : 1;

        foreach (var itemBlock in list)
        {
            if (itemBlock is not ListItemBlock item) continue;

            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
            var bullet = new TextBlock
            {
                Text = list.IsOrdered ? $"{index}." : "•",
                Foreground = new SolidColorBrush(Color.FromArgb(255, 6, 182, 212)),
                FontSize = 14,
                MinWidth = 18,
            };
            row.Children.Add(bullet);

            var content = new StackPanel { Spacing = 2 };
            foreach (var child in item)
            {
                var element = RenderBlock(child);
                if (element is not null) content.Children.Add(element);
            }
            row.Children.Add(content);

            panel.Children.Add(row);
            index++;
        }

        return panel;
    }

    private static FrameworkElement RenderQuote(QuoteBlock quote)
    {
        var inner = new StackPanel { Spacing = 4 };
        foreach (var child in quote)
        {
            var element = RenderBlock(child);
            if (element is not null) inner.Children.Add(element);
        }

        return new Border
        {
            BorderThickness = new Thickness(3, 0, 0, 0),
            BorderBrush = new SolidColorBrush(Color.FromArgb(255, 6, 182, 212)),
            Padding = new Thickness(12, 2, 0, 2),
            Child = inner,
        };
    }

    private static FrameworkElement RenderTable(Table table)
    {
        var grid = new Grid();
        var rowIndex = 0;
        var maxColumns = 0;

        foreach (var rowObj in table)
        {
            if (rowObj is not TableRow row) continue;
            maxColumns = Math.Max(maxColumns, row.Count);
        }
        for (var c = 0; c < maxColumns; c++)
        {
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        }

        foreach (var rowObj in table)
        {
            if (rowObj is not TableRow row) continue;
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var colIndex = 0;
            foreach (var cellObj in row)
            {
                if (cellObj is not TableCell cell) continue;
                var cellPanel = new StackPanel();
                foreach (var child in cell)
                {
                    var element = RenderBlock(child);
                    if (element is not null) cellPanel.Children.Add(element);
                }

                var border = new Border
                {
                    BorderThickness = new Thickness(0, 0, 1, 1),
                    BorderBrush = new SolidColorBrush(Color.FromArgb(40, 255, 255, 255)),
                    Padding = new Thickness(8, 4, 8, 4),
                    Child = cellPanel,
                };
                if (row.IsHeader)
                {
                    border.Background = new SolidColorBrush(Color.FromArgb(20, 255, 255, 255));
                }

                Grid.SetRow(border, rowIndex);
                Grid.SetColumn(border, colIndex);
                grid.Children.Add(border);
                colIndex++;
            }
            rowIndex++;
        }

        return new ScrollViewer
        {
            HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollMode = ScrollMode.Disabled,
            Content = grid,
            Margin = new Thickness(0, 4, 0, 4),
        };
    }
}
