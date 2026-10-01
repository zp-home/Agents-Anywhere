package com.agentsanywhere.app.feature.voice

import org.junit.Assert.assertEquals
import org.junit.Test

class VoiceCommandParserTest {
    @Test
    fun recognisesStandaloneCommandsWithFillers() {
        assertEquals(VoiceCommand.Interrupt, VoiceCommandParser.parse("停止"))
        assertEquals(VoiceCommand.Interrupt, VoiceCommandParser.parse("嗯，停一下吧。"))
        assertEquals(VoiceCommand.Interrupt, VoiceCommandParser.parse("别做了"))
        assertEquals(VoiceCommand.Approve, VoiceCommandParser.parse("同意。"))
        assertEquals(VoiceCommand.Approve, VoiceCommandParser.parse("可以吧"))
        assertEquals(VoiceCommand.Reject, VoiceCommandParser.parse("不行"))
        assertEquals(VoiceCommand.Reject, VoiceCommandParser.parse("拒绝！"))
        assertEquals(VoiceCommand.Repeat, VoiceCommandParser.parse("再说一遍"))
        assertEquals(VoiceCommand.HangUp, VoiceCommandParser.parse("挂了"))
        assertEquals(VoiceCommand.HangUp, VoiceCommandParser.parse("结束通话"))
        assertEquals(VoiceCommand.HangUp, VoiceCommandParser.parse("Hang up."))
    }

    @Test
    fun longerSentencesStayMessages() {
        assertEquals(VoiceCommand.Message("停止那个开发服务器"), VoiceCommandParser.parse("停止那个开发服务器"))
        assertEquals(VoiceCommand.Message("可以帮我看看测试为什么挂了吗"), VoiceCommandParser.parse("可以帮我看看测试为什么挂了吗"))
        assertEquals(VoiceCommand.Message("你好"), VoiceCommandParser.parse("你好"))
    }

    @Test
    fun blankInputIsAnEmptyMessage() {
        assertEquals(VoiceCommand.Message(""), VoiceCommandParser.parse("  。 "))
    }
}
