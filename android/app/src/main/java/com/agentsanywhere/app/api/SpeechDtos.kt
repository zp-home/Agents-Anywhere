package com.agentsanywhere.app.api

import org.json.JSONObject

data class RemoteSpeechStatus(
    val available: Boolean,
    val engine: String?,
    val languages: List<String>,
    val sampleRate: Int,
    val maxDurationSeconds: Int,
    val maxBytes: Int,
)

data class RemoteSpeechTranscription(
    val text: String,
    val language: String?,
    val durationMs: Long,
)

internal fun JSONObject.toRemoteSpeechStatus(): RemoteSpeechStatus {
    val languages = optJSONArray("languages")
    return RemoteSpeechStatus(
        available = optBoolean("available", false),
        engine = optString("engine", "").takeIf { it.isNotBlank() && it != "null" },
        languages = (0 until (languages?.length() ?: 0)).mapNotNull { languages?.optString(it)?.takeIf(String::isNotBlank) },
        sampleRate = optInt("sampleRate", 16_000),
        maxDurationSeconds = optInt("maxDurationSeconds", 60),
        maxBytes = optInt("maxBytes", 2 * 1024 * 1024),
    )
}

internal fun JSONObject.toRemoteSpeechTranscription(): RemoteSpeechTranscription = RemoteSpeechTranscription(
    text = optString("text", "").trim(),
    language = optString("language", "").takeIf { it.isNotBlank() && it != "null" },
    durationMs = optLong("durationMs", 0L),
)
