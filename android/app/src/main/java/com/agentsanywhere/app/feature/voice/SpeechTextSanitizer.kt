package com.agentsanywhere.app.feature.voice

import org.commonmark.ext.gfm.strikethrough.StrikethroughExtension
import org.commonmark.ext.gfm.tables.TableBlock
import org.commonmark.ext.gfm.tables.TablesExtension
import org.commonmark.node.Code
import org.commonmark.node.FencedCodeBlock
import org.commonmark.node.HardLineBreak
import org.commonmark.node.Heading
import org.commonmark.node.HtmlBlock
import org.commonmark.node.HtmlInline
import org.commonmark.node.Image
import org.commonmark.node.IndentedCodeBlock
import org.commonmark.node.ListItem
import org.commonmark.node.Node
import org.commonmark.node.Paragraph
import org.commonmark.node.SoftLineBreak
import org.commonmark.node.Text
import org.commonmark.node.ThematicBreak
import org.commonmark.parser.Parser

data class SpeechTextLabels(
    val codeOmitted: String,
    val tableOmitted: String,
    val truncated: String,
)

/**
 * Turns an agent's markdown reply into text that sounds reasonable when read by TTS:
 * code blocks and tables are replaced by a short spoken marker, markdown syntax and
 * URLs are dropped, and long replies are cut at a sentence boundary.
 */
object SpeechTextSanitizer {
    const val DEFAULT_MAX_CHARS = 600
    private const val MAX_INLINE_CODE_CHARS = 40

    private val parser: Parser = Parser.builder()
        .extensions(listOf(TablesExtension.create(), StrikethroughExtension.create()))
        .build()
    private val urlPattern = Regex("https?://\\S+")
    private val sentenceEnd = charArrayOf('。', '！', '？', '；', '.', '!', '?', ';', '\n')

    fun sanitize(markdown: String, labels: SpeechTextLabels, maxChars: Int = DEFAULT_MAX_CHARS): String {
        if (markdown.isBlank()) return ""
        val out = StringBuilder()
        render(parser.parse(markdown), out, labels)
        val flat = out.toString()
            .replace(urlPattern, "")
            .replace(Regex("[ \\t\\u00A0]+"), " ")
            .replace(Regex(" ?\\n ?"), "\n")
            .replace(Regex("\\n+"), "\n")
            .replace(Regex("([。！？.!?])\\s*[。.]+"), "$1")
            .trim()
        return truncate(flat, labels, maxChars)
    }

    private fun render(node: Node, out: StringBuilder, labels: SpeechTextLabels) {
        when (node) {
            is FencedCodeBlock, is IndentedCodeBlock -> out.sentence(labels.codeOmitted)
            is TableBlock -> out.sentence(labels.tableOmitted)
            is HtmlBlock, is HtmlInline, is ThematicBreak, is Image -> Unit
            is Text -> out.append(node.literal)
            is Code -> out.append(spokenInlineCode(node.literal))
            is SoftLineBreak -> out.append(' ')
            is HardLineBreak -> out.sentenceBreak()
            is Heading, is Paragraph, is ListItem -> {
                renderChildren(node, out, labels)
                out.sentenceBreak()
            }
            else -> renderChildren(node, out, labels)
        }
    }

    private fun renderChildren(node: Node, out: StringBuilder, labels: SpeechTextLabels) {
        var child = node.firstChild
        while (child != null) {
            render(child, out, labels)
            child = child.next
        }
    }

    private fun spokenInlineCode(literal: String): String {
        val trimmed = literal.trim()
        val shortened = if ('/' in trimmed || '\\' in trimmed) {
            trimmed.trimEnd('/', '\\').substringAfterLast('/').substringAfterLast('\\')
        } else {
            trimmed
        }
        return if (shortened.length > MAX_INLINE_CODE_CHARS) "" else shortened
    }

    private fun StringBuilder.sentence(text: String) {
        sentenceBreak()
        append(text)
        sentenceBreak()
    }

    private fun StringBuilder.sentenceBreak() {
        val last = trimEnd().lastOrNull() ?: return
        if (last in sentenceEnd || last == '：' || last == ':') {
            append('\n')
            return
        }
        append(if (last.code < 0x80) ".\n" else "。\n")
    }

    private fun truncate(text: String, labels: SpeechTextLabels, maxChars: Int): String {
        if (text.length <= maxChars) return text
        val window = text.substring(0, maxChars)
        val cut = window.lastIndexOfAny(sentenceEnd)
        val kept = if (cut >= maxChars / 2) window.substring(0, cut + 1) else window
        return kept.trimEnd() + "\n" + labels.truncated
    }
}
