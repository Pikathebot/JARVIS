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
            RenderBlocks(Markdown.Parse(remainder, Pipeline), root);
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
            Style = Themes.JarvisTheme.Style("BodyText"),
            Foreground = Themes.JarvisTheme.Brush("LabelSecondary"),
        };
        return expander;
    }

    /// <summary>Gap (DIPs) above a paragraph, and above a list item after a list's first.</summary>
    private const double ParagraphGap = 6;
    private const double ListItemGap = 2;

    /// <summary>
    /// Where rendered blocks land. Consecutive prose -- paragraphs, headings, list items -- goes
    /// into ONE RichTextBlock as Paragraphs, so a drag can select across the whole answer and
    /// Ctrl+C copies it. With one TextBlock per block (until 2026-10-20) a selection stopped at
    /// the edge of its line or bullet. Anything that is not text (a code block, a table, a
    /// quote's bar, a rule) closes the current RichTextBlock and the next prose opens a new one.
    /// </summary>
    private sealed class BlockSink(Panel panel)
    {
        private RichTextBlock? _prose;

        public void AddParagraph(Paragraph paragraph, double gapAbove)
        {
            if (_prose is null)
            {
                // MessageText (Typography.xaml) -- a TextBlock style, which a RichTextBlock can't take.
                _prose = new RichTextBlock
                {
                    FontFamily = (FontFamily)Application.Current.Resources["TextFontFamily"],
                    FontSize = 15,
                    LineHeight = 22,
                    LineStackingStrategy = LineStackingStrategy.BlockLineHeight,
                    TextLineBounds = TextLineBounds.Full,
                    Foreground = Themes.JarvisTheme.Brush("Label"),
                    TextWrapping = TextWrapping.Wrap,
                    IsTextSelectionEnabled = true,
                };
                panel.Children.Add(_prose);
                gapAbove = 0; // the panel's spacing separates it from whatever came before
            }
            var m = paragraph.Margin;
            paragraph.Margin = new Thickness(m.Left, m.Top + gapAbove, m.Right, m.Bottom);
            _prose.Blocks.Add(paragraph);
        }

        public void AddElement(FrameworkElement element, double indent)
        {
            _prose = null;
            if (indent > 0)
            {
                var m = element.Margin;
                element.Margin = new Thickness(m.Left + indent, m.Top, m.Right, m.Bottom);
            }
            panel.Children.Add(element);
        }
    }

    private static void RenderBlocks(IEnumerable<Markdig.Syntax.Block> blocks, Panel panel)
    {
        var sink = new BlockSink(panel);
        foreach (var block in blocks) RenderBlock(block, sink, 0);
    }

    private static void RenderBlock(Markdig.Syntax.Block block, BlockSink sink, double indent)
    {
        switch (block)
        {
            case HeadingBlock heading:
                if (heading.Inline is not null)
                    sink.AddParagraph(HeadingParagraph(heading, indent), (heading.Level <= 2 ? 8 : 4) + ParagraphGap);
                break;
            case Markdig.Syntax.FencedCodeBlock fenced:
                sink.AddElement(RenderCodeBlock(fenced), indent);
                break;
            case Markdig.Syntax.CodeBlock code:
                sink.AddElement(RenderCodeBlock(code, language: ""), indent);
                break;
            case ListBlock list:
                RenderList(list, sink, indent);
                break;
            case QuoteBlock quote:
                sink.AddElement(RenderQuote(quote), indent);
                break;
            case ThematicBreakBlock:
                sink.AddElement(new Border { Height = 1, Background = Themes.JarvisTheme.Brush("Separator"), Margin = new Thickness(0, 8, 0, 8) }, indent);
                break;
            case Table table:
                sink.AddElement(RenderTable(table), indent);
                break;
            default:
                // ParagraphBlock, and any other leaf that carries text.
                if (block is LeafBlock leaf && leaf.Inline is not null)
                    sink.AddParagraph(TextParagraph(leaf.Inline, indent), ParagraphGap);
                break;
        }
    }

    private static Paragraph TextParagraph(ContainerInline inlines, double indent)
    {
        var paragraph = new Paragraph { Margin = new Thickness(indent, 0, 0, 0) };
        AppendInlines(paragraph.Inlines, inlines);
        return paragraph;
    }

    /// <summary>Design system: # title-1, ## title-2, ### title-3, #### and deeper headline --
    /// the values of those TextBlock styles in Typography.xaml.</summary>
    private static Paragraph HeadingParagraph(HeadingBlock heading, double indent)
    {
        var (display, size, lineHeight) = heading.Level switch
        {
            1 => (true, 22.0, 28.0),
            2 => (true, 20.0, 25.0),
            3 => (false, 17.0, 22.0),
            _ => (false, 15.0, 20.0),
        };
        var paragraph = TextParagraph(heading.Inline!, indent);
        if (display) paragraph.FontFamily = (FontFamily)Application.Current.Resources["DisplayFontFamily"];
        paragraph.FontSize = size;
        paragraph.LineHeight = lineHeight;
        paragraph.FontWeight = FontWeights.SemiBold;
        paragraph.Margin = new Thickness(indent, 0, 0, 4);
        return paragraph;
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
                        FontFamily = (FontFamily)Application.Current.Resources["MonoFontFamily"],
                        FontSize = 13,
                        Foreground = Themes.JarvisTheme.Brush("AccentText"),
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
            Background = Themes.JarvisTheme.Brush("SurfaceCode"),
            CornerRadius = new CornerRadius(10),
            BorderThickness = new Thickness(1),
            BorderBrush = Themes.JarvisTheme.Brush("Separator"),
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
            Style = Themes.JarvisTheme.Style("CaptionText"),
            Foreground = Themes.JarvisTheme.Brush("LabelSecondary"),
            VerticalAlignment = VerticalAlignment.Center,
        });

        var copyButton = new Button { Content = "Copy", Padding = new Thickness(8, 2, 8, 2) };
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
            Style = Themes.JarvisTheme.Style("CodeBlockText"),
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

    /// <summary>
    /// A list item is a Paragraph with a hanging indent: the marker sits in the first line's
    /// negative indent and wrapped lines line up after it. The marker is real text, so a copied
    /// list keeps its bullets and numbers.
    /// </summary>
    private static void RenderList(ListBlock list, BlockSink sink, double indent)
    {
        const double MarkerWidth = 20;
        var itemIndent = indent + 4 + MarkerWidth;
        var index = list.OrderedStart is not null && int.TryParse(list.OrderedStart, out var start) ? start : 1;
        var first = true;

        foreach (var itemBlock in list)
        {
            if (itemBlock is not ListItemBlock item) continue;

            var marked = false;
            foreach (var child in item)
            {
                if (!marked && child is ParagraphBlock { Inline: not null } para)
                {
                    var paragraph = TextParagraph(para.Inline, itemIndent);
                    paragraph.TextIndent = -MarkerWidth;
                    paragraph.Inlines.Insert(0, MarkerRun(list.IsOrdered ? $"{index}." : "•", MarkerWidth));
                    sink.AddParagraph(paragraph, first ? ParagraphGap : ListItemGap);
                    marked = true;
                }
                else
                {
                    RenderBlock(child, sink, itemIndent);
                }
                first = false;
            }
            index++;
        }
    }

    private static double _thinSpaceWidth;

    /// <summary>
    /// The marker padded with thin spaces out to <paramref name="width"/>, so the text after it
    /// starts where wrapped lines do (a RichTextBlock has no tab stops). Widths are measured with
    /// a loose TextBlock, which measures without being in the tree.
    /// </summary>
    private static Run MarkerRun(string marker, double width)
    {
        static double Measure(string text)
        {
            var probe = new TextBlock
            {
                Text = text,
                FontSize = 15,
                FontFamily = (FontFamily)Application.Current.Resources["TextFontFamily"],
            };
            probe.Measure(new Windows.Foundation.Size(double.PositiveInfinity, double.PositiveInfinity));
            return probe.DesiredSize.Width;
        }

        if (_thinSpaceWidth <= 0) _thinSpaceWidth = Math.Max(1, Measure("    ") / 4);
        var pad = Math.Max(1, (int)Math.Round((width - Measure(marker)) / _thinSpaceWidth));
        return new Run
        {
            Text = marker + new string(' ', pad),
            Foreground = Themes.JarvisTheme.Brush("LabelSecondary"),
        };
    }

    private static FrameworkElement RenderQuote(QuoteBlock quote)
    {
        var inner = new StackPanel { Spacing = 4 };
        RenderBlocks(quote, inner);

        return new Border
        {
            // A neutral bar: accent is reserved for selection and live state.
            BorderThickness = new Thickness(3, 0, 0, 0),
            BorderBrush = Themes.JarvisTheme.Brush("Separator"),
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
                RenderBlocks(cell, cellPanel);

                var border = new Border
                {
                    BorderThickness = new Thickness(0, 0, 1, 1),
                    BorderBrush = Themes.JarvisTheme.Brush("Separator"),
                    Padding = new Thickness(8, 4, 8, 4),
                    Child = cellPanel,
                };
                if (row.IsHeader)
                {
                    border.Background = Themes.JarvisTheme.Brush("FillTertiary");
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
