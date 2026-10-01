package com.agentsanywhere.app.feature.voice

import com.agentsanywhere.app.api.toRemoteSpeechStatus
import com.agentsanywhere.app.api.toRemoteSpeechTranscription
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeechDtosTest {
    @Test
    fun parsesAvailableStatus() {
        val status = JSONObject(
            """{"available":true,"engine":"sensevoice-small","languages":["auto","zh"],"sampleRate":16000,"maxDurationSeconds":60,"maxBytes":2097152}""",
        ).toRemoteSpeechStatus()

        assertTrue(status.available)
        assertEquals("sensevoice-small", status.engine)
        assertEquals(listOf("auto", "zh"), status.languages)
        assertEquals(60, status.maxDurationSeconds)
    }

    @Test
    fun parsesUnavailableStatusWithNullEngine() {
        val status = JSONObject("""{"available":false,"engine":null,"languages":[]}""").toRemoteSpeechStatus()

        assertFalse(status.available)
        assertNull(status.engine)
        assertEquals(emptyList<String>(), status.languages)
        assertEquals(16_000, status.sampleRate)
    }

    @Test
    fun parsesTranscription() {
        val result = JSONObject("""{"text":" 跑一下测试 ","language":"zh","durationMs":2380}""").toRemoteSpeechTranscription()

        assertEquals("跑一下测试", result.text)
        assertEquals("zh", result.language)
        assertEquals(2380L, result.durationMs)
    }
}
