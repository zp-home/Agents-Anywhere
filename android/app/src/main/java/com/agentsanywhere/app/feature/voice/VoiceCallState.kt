package com.agentsanywhere.app.feature.voice

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class VoiceCallPhase {
    Connecting,
    Listening,
    Sending,
    AgentWorking,
    AwaitingApproval,
    Speaking,
    Standby,
    Ended,
}

data class VoiceCallUiState(
    val sessionId: String,
    val title: String,
    val phase: VoiceCallPhase = VoiceCallPhase.Connecting,
    val partialTranscript: String = "",
    val listeningHint: String = "",
    val lastHeard: String = "",
    val lastSpoken: String = "",
    val connected: Boolean = false,
    val errorMessage: String? = null,
    val recognitionMode: VoiceRecognitionMode = VoiceRecognitionMode.System,
    val systemRecognitionAvailable: Boolean = true,
    val serverRecognitionAvailable: Boolean = false,
)

/** Spoken prompts and labels; built from string resources by the service. */
data class VoicePhrases(
    val messagePrefix: String,
    val connected: String,
    val agentAlreadyRunning: String,
    val stillHere: String,
    val standby: String,
    val interrupted: String,
    val nothingRunning: String,
    val steerUnavailable: String,
    val approvalPrompt: String,
    val approvalNeedsScreen: String,
    val approvalAnswerHint: String,
    val approvalDone: String,
    val actionFailed: String,
    val turnFailed: String,
    val noReply: String,
    val workSummaryFiles: String,
    val workSummaryCommands: String,
    val notStarted: String,
    val sendFailed: String,
    val nothingToRepeat: String,
    val hangUp: String,
    val recognizerUnavailable: String,
    val noSpeechHeard: String,
    val recognitionFailed: String,
    val labels: SpeechTextLabels,
)

/** Current voice call, observed by the session screen. At most one call at a time. */
object VoiceCallRegistry {
    private val mutable = MutableStateFlow<VoiceCallUiState?>(null)
    val state: StateFlow<VoiceCallUiState?> = mutable.asStateFlow()

    private val screenVisible = MutableStateFlow(false)

    /** True while the full-screen call view is on screen; otherwise the floating bubble shows. */
    val callScreenVisible: StateFlow<Boolean> = screenVisible.asStateFlow()

    private val showRequests = MutableStateFlow(0)

    /** Incremented when the user asks to return to the call (bubble tap). */
    val showCallRequests: StateFlow<Int> = showRequests.asStateFlow()

    fun setCallScreenVisible(visible: Boolean) {
        screenVisible.value = visible
    }

    fun requestShowCall() {
        showRequests.value += 1
    }

    internal fun publish(state: VoiceCallUiState?) {
        mutable.value = state
    }

    internal fun update(transform: (VoiceCallUiState) -> VoiceCallUiState) {
        val current = mutable.value ?: return
        mutable.value = transform(current)
    }
}

sealed interface SpeechInputResult {
    data class Final(val text: String) : SpeechInputResult
    data object NoMatch : SpeechInputResult
    data class Failed(val fatal: Boolean, val reason: String) : SpeechInputResult
}

interface SpeechInput {
    /**
     * Listens for one utterance. [onPartial] receives interim transcripts; [onHint]
     * receives status text such as "recording 2.4 s" for inputs without live transcripts.
     */
    suspend fun listen(onPartial: (String) -> Unit, onHint: (String) -> Unit): SpeechInputResult

    fun cancel()

    fun release()
}

interface SpeechOutput {
    /** Speaks [text] and suspends until playback finishes or [stop] is called. */
    suspend fun speak(text: String)

    fun stop()

    fun release()
}

interface VoiceCues {
    fun listening()

    fun working()

    fun acknowledged()

    fun release()
}
