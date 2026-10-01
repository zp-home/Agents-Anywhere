package com.agentsanywhere.app.feature.voice

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.SystemClock
import android.util.Log
import com.agentsanywhere.app.api.ApiException
import com.agentsanywhere.app.api.SpeechApi
import com.agentsanywhere.app.feature.auth.AuthSessionReader
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.util.Locale
import kotlin.coroutines.coroutineContext

enum class VoiceRecognitionMode(val storageValue: String) {
    System("system"),
    Server("server"),
    ;

    companion object {
        fun fromStorage(value: String?): VoiceRecognitionMode =
            entries.firstOrNull { it.storageValue == value } ?: System
    }
}

class VoiceRecognitionPreferences(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences("voice_call", Context.MODE_PRIVATE)

    fun read(): VoiceRecognitionMode = VoiceRecognitionMode.fromStorage(preferences.getString(KEY_MODE, null))

    fun write(mode: VoiceRecognitionMode) {
        preferences.edit().putString(KEY_MODE, mode.storageValue).apply()
    }

    private companion object {
        const val KEY_MODE = "recognition_mode"
    }
}

/** Delegates to the recognizer picked for the call; a switch applies to the next utterance. */
class SwitchableSpeechInput(
    private val system: SpeechInput,
    private val server: SpeechInput,
    initial: VoiceRecognitionMode,
) : SpeechInput {
    @Volatile
    var mode: VoiceRecognitionMode = initial

    override suspend fun listen(onPartial: (String) -> Unit, onHint: (String) -> Unit): SpeechInputResult =
        (if (mode == VoiceRecognitionMode.Server) server else system).listen(onPartial, onHint)

    override fun cancel() {
        system.cancel()
        server.cancel()
    }

    override fun release() {
        system.release()
        server.release()
    }
}

data class ServerSpeechLabels(
    val listening: String,
    /** Format string with one `%1$.1f` seconds argument. */
    val recording: String,
    val recognizing: String,
)

/**
 * Records one utterance with [AudioRecord], cuts it with [EnergyVad] and sends it to
 * the Server's speech endpoint (SenseVoice). Every utterance is summarized in logcat
 * under the `AAVoice` tag (levels, end reason, upload result) so field problems can be
 * diagnosed with `adb logcat -s AAVoice`.
 */
class ServerSpeechInput(
    private val speechApi: SpeechApi,
    private val sessionStore: AuthSessionReader,
    private val labels: ServerSpeechLabels,
) : SpeechInput {
    @SuppressLint("MissingPermission") // RECORD_AUDIO is granted before the call service starts.
    override suspend fun listen(onPartial: (String) -> Unit, onHint: (String) -> Unit): SpeechInputResult =
        withContext(Dispatchers.IO) {
            val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
            if (minBuffer <= 0) {
                Log.w(TAG, "AudioRecord.getMinBufferSize returned $minBuffer")
                return@withContext SpeechInputResult.Failed(fatal = true, reason = "audio input unavailable")
            }
            val record = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE,
                    CHANNEL,
                    ENCODING,
                    maxOf(minBuffer, FRAME_SAMPLES * 2 * 10),
                )
            } catch (error: SecurityException) {
                Log.w(TAG, "AudioRecord denied", error)
                return@withContext SpeechInputResult.Failed(fatal = true, reason = "microphone permission")
            }
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                Log.w(TAG, "AudioRecord state=${record.state}")
                record.release()
                return@withContext SpeechInputResult.Failed(fatal = true, reason = "microphone unavailable")
            }
            val vad = EnergyVad(frameMs = FRAME_MS)
            onHint(labels.listening)
            val captured = try {
                capture(record, vad, onHint)
            } finally {
                runCatching { record.stop() }
                record.release()
            }
            val stats = vad.stats
            when (captured) {
                is EnergyVad.Result.Utterance -> {
                    onHint(labels.recognizing)
                    upload(captured, stats)
                }
                is CaptureFailure -> {
                    Log.w(TAG, "capture failed: ${captured.reason} ${stats.describe()}")
                    SpeechInputResult.Failed(fatal = false, reason = captured.reason)
                }
                else -> {
                    Log.i(TAG, "no speech ${stats.describe()}")
                    SpeechInputResult.NoMatch
                }
            }
        }

    private suspend fun capture(record: AudioRecord, vad: EnergyVad, onHint: (String) -> Unit): Any {
        record.startRecording()
        if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            return CaptureFailure("microphone busy (recordingState=${record.recordingState})")
        }
        var lastHintMs = -1
        while (true) {
            coroutineContext.ensureActive()
            val frame = ShortArray(FRAME_SAMPLES)
            var filled = 0
            while (filled < frame.size) {
                val read = record.read(frame, filled, frame.size - filled)
                if (read < 0) return CaptureFailure("audio read error $read")
                filled += read
            }
            val result = vad.accept(frame)
            if (result != EnergyVad.Result.Continue) return result
            val speechMs = vad.stats.speechMs
            if (vad.stats.speechStartMs != null && speechMs / HINT_INTERVAL_MS != lastHintMs / HINT_INTERVAL_MS) {
                lastHintMs = speechMs
                onHint(labels.recording.format(Locale.ROOT, speechMs / 1000.0))
            }
        }
    }

    private fun upload(utterance: EnergyVad.Result.Utterance, stats: EnergyVad.Stats): SpeechInputResult {
        val serverUrl = sessionStore.readServerUrl()
        val token = sessionStore.readAccessToken()
        if (serverUrl.isBlank() || token.isBlank()) {
            return SpeechInputResult.Failed(fatal = true, reason = "not signed in")
        }
        val audioMs = utterance.samples.size * 1000L / SAMPLE_RATE
        val startedAt = SystemClock.elapsedRealtime()
        return runCatching { speechApi.transcribe(serverUrl, token, WavEncoder.pcm16Mono(utterance.samples, SAMPLE_RATE)) }
            .fold(
                onSuccess = { result ->
                    Log.i(
                        TAG,
                        "utterance end=${utterance.endReason} audio=${audioMs}ms ${stats.describe()} " +
                            "upload=ok in ${SystemClock.elapsedRealtime() - startedAt}ms textLength=${result.text.length}",
                    )
                    if (result.text.isBlank()) SpeechInputResult.NoMatch else SpeechInputResult.Final(result.text)
                },
                onFailure = { error ->
                    val status = (error as? ApiException)?.statusCode
                    Log.w(
                        TAG,
                        "utterance end=${utterance.endReason} audio=${audioMs}ms ${stats.describe()} " +
                            "upload=failed status=$status in ${SystemClock.elapsedRealtime() - startedAt}ms: ${error.message}",
                    )
                    val reason = listOfNotNull(status?.let { "HTTP $it" }, error.message).joinToString(" ")
                    SpeechInputResult.Failed(fatal = false, reason = reason.ifBlank { "upload failed" })
                },
            )
    }

    override fun cancel() = Unit

    override fun release() = Unit

    private class CaptureFailure(val reason: String)

    private companion object {
        const val TAG = "AAVoice"
        const val SAMPLE_RATE = 16_000
        const val FRAME_MS = 20
        const val FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000
        const val HINT_INTERVAL_MS = 500
        const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
    }
}

private fun EnergyVad.Stats.describe(): String =
    "elapsed=${elapsedMs}ms speech=${speechMs}ms floor=${noiseFloor.toInt()} " +
        "start>${startThreshold.toInt()} end<${endThreshold.toInt()} peak=${peak.toInt()} maxLevel=${maxLevel.toInt()}"
