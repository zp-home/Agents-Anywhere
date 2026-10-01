package com.agentsanywhere.app.feature.voice

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import kotlinx.coroutines.CancellableContinuation
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import java.util.Locale
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlin.coroutines.resume

/** Recognition through the phone's own speech service (Google, Xiaomi, Huawei, ...). */
class SystemSpeechInput(
    private val context: Context,
    private val locale: Locale,
) : SpeechInput {
    private val mainHandler = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null

    override suspend fun listen(onPartial: (String) -> Unit, onHint: (String) -> Unit): SpeechInputResult = withContext<SpeechInputResult>(Dispatchers.Main.immediate) {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            return@withContext SpeechInputResult.Failed(fatal = true, reason = "no recognition service")
        }
        val active = recognizer ?: SpeechRecognizer.createSpeechRecognizer(context).also { recognizer = it }
        suspendCancellableCoroutine { continuation ->
            fun finish(result: SpeechInputResult) {
                if (continuation.isActive) continuation.resume(result)
            }
            active.setRecognitionListener(object : RecognitionListener {
                override fun onResults(results: Bundle?) {
                    val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                        ?.firstOrNull()
                        ?.trim()
                        .orEmpty()
                    finish(if (text.isEmpty()) SpeechInputResult.NoMatch else SpeechInputResult.Final(text))
                }

                override fun onPartialResults(partialResults: Bundle?) {
                    partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                        ?.firstOrNull()
                        ?.takeIf(String::isNotBlank)
                        ?.let(onPartial)
                }

                override fun onError(error: Int) {
                    finish(mapError(error))
                }

                override fun onReadyForSpeech(params: Bundle?) = Unit
                override fun onBeginningOfSpeech() = Unit
                override fun onRmsChanged(rmsdB: Float) = Unit
                override fun onBufferReceived(buffer: ByteArray?) = Unit
                override fun onEndOfSpeech() = Unit
                override fun onEvent(eventType: Int, params: Bundle?) = Unit
            })
            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, locale.toLanguageTag())
                putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, END_OF_SPEECH_SILENCE_MS)
                putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            }
            active.startListening(intent)
            continuation.invokeOnCancellation { mainHandler.post { active.cancel() } }
        }
    }

    private fun mapError(error: Int): SpeechInputResult = when (error) {
        SpeechRecognizer.ERROR_NO_MATCH,
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT,
        -> SpeechInputResult.NoMatch
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
            SpeechInputResult.Failed(fatal = true, reason = "microphone permission")
        ERROR_LANGUAGE_NOT_SUPPORTED ->
            SpeechInputResult.Failed(fatal = true, reason = "language ${locale.toLanguageTag()} not supported")
        else -> {
            // Busy/client errors usually clear up with a fresh recognizer instance.
            recognizer?.destroy()
            recognizer = null
            SpeechInputResult.Failed(fatal = false, reason = "error $error")
        }
    }

    override fun cancel() {
        mainHandler.post { recognizer?.cancel() }
    }

    override fun release() {
        mainHandler.post {
            recognizer?.destroy()
            recognizer = null
        }
    }

    private companion object {
        const val END_OF_SPEECH_SILENCE_MS = 1_500L
        const val ERROR_LANGUAGE_NOT_SUPPORTED = 12
    }
}

/** Reads replies with the phone's text-to-speech engine. */
class TtsSpeechOutput(
    context: Context,
    private val locale: Locale,
) : SpeechOutput {
    private val ready = CompletableDeferred<Boolean>()
    private val pending = ConcurrentHashMap<String, CancellableContinuation<Unit>>()
    private val tts = TextToSpeech(context.applicationContext) { status ->
        ready.complete(status == TextToSpeech.SUCCESS)
    }

    init {
        tts.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build(),
        )
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = Unit

            override fun onDone(utteranceId: String?) = finish(utteranceId)

            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) = finish(utteranceId)

            override fun onError(utteranceId: String?, errorCode: Int) = finish(utteranceId)

            override fun onStop(utteranceId: String?, interrupted: Boolean) = finish(utteranceId)
        })
    }

    private var languageApplied = false

    override suspend fun speak(text: String) {
        if (!ready.await()) return
        if (!languageApplied) {
            languageApplied = true
            if (tts.isLanguageAvailable(locale) >= TextToSpeech.LANG_AVAILABLE) tts.language = locale
        }
        suspendCancellableCoroutine { continuation ->
            val id = UUID.randomUUID().toString()
            pending[id] = continuation
            continuation.invokeOnCancellation {
                pending.remove(id)
                tts.stop()
            }
            if (tts.speak(text, TextToSpeech.QUEUE_FLUSH, Bundle(), id) != TextToSpeech.SUCCESS) finish(id)
        }
    }

    private fun finish(utteranceId: String?) {
        val continuation = utteranceId?.let(pending::remove) ?: return
        if (continuation.isActive) continuation.resume(Unit)
    }

    override fun stop() {
        tts.stop()
        pending.keys.toList().forEach(::finish)
    }

    override fun release() {
        stop()
        tts.shutdown()
    }
}

/** Short tones so a user with eyes closed knows what the call is doing. */
class ToneVoiceCues : VoiceCues {
    private val generator = runCatching { ToneGenerator(AudioManager.STREAM_MUSIC, TONE_VOLUME) }.getOrNull()

    override fun listening() {
        generator?.startTone(ToneGenerator.TONE_PROP_BEEP, 120)
    }

    override fun working() {
        generator?.startTone(ToneGenerator.TONE_SUP_PIP, 60)
    }

    override fun acknowledged() {
        generator?.startTone(ToneGenerator.TONE_PROP_ACK, 150)
    }

    override fun release() {
        generator?.release()
    }

    private companion object {
        const val TONE_VOLUME = 60
    }
}
