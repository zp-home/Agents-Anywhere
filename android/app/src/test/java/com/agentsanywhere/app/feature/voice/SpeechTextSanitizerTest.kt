package com.agentsanywhere.app.feature.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeechTextSanitizerTest {
    private val labels = SpeechTextLabels(
        codeOmitted = "（代码已省略）",
        tableOmitted = "（表格已省略）",
        truncated = "后面还有内容。",
    )

    @Test
    fun replacesCodeBlocksAndTables() {
        val markdown = """
            ## 结果
            已修复登录问题。

            ```kotlin
            fun main() = println("hi")
            ```

            | a | b |
            |---|---|
            | 1 | 2 |
        """.trimIndent()

        val spoken = SpeechTextSanitizer.sanitize(markdown, labels)

        assertTrue(spoken, spoken.contains("结果"))
        assertTrue(spoken, spoken.contains("已修复登录问题"))
        assertTrue(spoken, spoken.contains("（代码已省略）"))
        assertTrue(spoken, spoken.contains("（表格已省略）"))
        assertFalse(spoken, spoken.contains("println"))
        assertFalse(spoken, spoken.contains("|"))
        assertFalse(spoken, spoken.contains("#"))
    }

    @Test
    fun dropsMarkdownSyntaxUrlsAndLongPaths() {
        val markdown = "改了 **两个** 文件：`app/src/main/java/com/example/LoginViewModel.kt`，" +
            "详见 [文档](https://example.com/docs)，也可以看 https://example.com/raw。"

        val spoken = SpeechTextSanitizer.sanitize(markdown, labels)

        assertTrue(spoken, spoken.contains("改了 两个 文件"))
        assertTrue(spoken, spoken.contains("LoginViewModel.kt"))
        assertTrue(spoken, spoken.contains("文档"))
        assertFalse(spoken, spoken.contains("app/src"))
        assertFalse(spoken, spoken.contains("https"))
        assertFalse(spoken, spoken.contains("*"))
    }

    @Test
    fun listItemsBecomeSeparateSentences() {
        val spoken = SpeechTextSanitizer.sanitize("- 第一步\n- 第二步", labels)

        assertEquals("第一步。\n第二步。", spoken)
    }

    @Test
    fun truncatesLongRepliesAtSentenceBoundary() {
        val sentence = "这是一个很长的句子用于测试截断逻辑。"
        val markdown = sentence.repeat(20)

        val spoken = SpeechTextSanitizer.sanitize(markdown, labels, maxChars = 100)

        assertTrue(spoken, spoken.endsWith("后面还有内容。"))
        val body = spoken.removeSuffix("后面还有内容。").trimEnd()
        assertTrue(body, body.length <= 100)
        assertTrue(body, body.endsWith("。"))
    }

    @Test
    fun blankInputStaysBlank() {
        assertEquals("", SpeechTextSanitizer.sanitize("   ", labels))
    }
}
