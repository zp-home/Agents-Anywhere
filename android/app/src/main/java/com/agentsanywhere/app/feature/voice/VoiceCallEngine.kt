package com.agentsanywhere.app.feature.voice

import com.agentsanywhere.app.feature.realtime.SessionRealtimeController
import com.agentsanywhere.app.feature.sessiondetail.MessageAuthor
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNotice
import com.agentsanywhere.app.feature.sessiondetail.SESSION_INTERRUPT_CAPABILITY
import com.agentsanywhere.app.feature.sessiondetail.SessionDetailController
import com.agentsanywhere.app.feature.sessiondetail.SessionDetailState
import com.agentsanywhere.app.feature.sessiondetail.SessionRuntimeStatus
import com.agentsanywhere.app.feature.sessiondetail.completeSnapshotLoad
import com.agentsanywhere.app.feature.sessiondetail.laterEventCursor
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.util.UUID

/**
 * Drives one hands-free call with a session: listen → send → wait for the agent →
 * read the reply aloud → listen again. Runs entirely on the main dispatcher; the
 * realtime callbacks hop back to main before touching [detail].
 */
class VoiceCallEngine(
    private val sessionId: String,
    private val controller: SessionDetailController,
    private val realtime: SessionRealtimeController,
    private val input: SpeechInput,
    private val output: SpeechOutput,
    private val cues: VoiceCues,
    private val phrases: VoicePhrases,
    private val onEnded: () -> Unit,
) {
    private var detail = SessionDetailState()
    private val wakeSignal = Channel<Unit>(Channel.CONFLATED)
    private val handledNotices = mutableSetOf<String>()
    private var lastSpoken = ""
    private var lastListenFailed = false
    private var job: Job? = null

    fun start(scope: CoroutineScope) {
        if (job != null) return
        job = scope.launch(Dispatchers.Main.immediate) {
            try {
                run()
            } catch (error: CancellationException) {
                throw error
            } catch (error: Throwable) {
                VoiceCallRegistry.update { it.copy(errorMessage = error.message) }
            }
            publishPhase(VoiceCallPhase.Ended)
            onEnded()
        }
    }

    /** Headset button / notification "talk" action: skip speech or open the mic. */
    fun wake() {
        wakeSignal.trySend(Unit)
    }

    fun stop() {
        job?.cancel()
        input.cancel()
        output.stop()
    }

    private suspend fun run() = coroutineScope {
        publishPhase(VoiceCallPhase.Connecting)
        val loaded = controller.loadInitialSnapshot(sessionId, emptyList(), null).getOrElse { error ->
            say(phrases.actionFailed.format(error.message.orEmpty()))
            return@coroutineScope
        }
        detail = loaded
        loaded.session?.title?.takeIf(String::isNotBlank)?.let { title ->
            VoiceCallRegistry.update { it.copy(title = title) }
        }
        val realtimeJob = startRealtime(this)
        try {
            val keepGoing = if (VoiceTurn.isActive(detail)) {
                say(phrases.agentAlreadyRunning)
                awaitTurn(baseline = lastUserOrderSeq(), sawActiveInitially = true)
            } else {
                say(phrases.connected)
                true
            }
            if (keepGoing) conversationLoop()
        } finally {
            realtimeJob.cancel()
        }
    }

    private suspend fun conversationLoop() {
        var emptyCount = 0
        while (true) {
            val heard = listen()
            when (heard) {
                is SpeechInputResult.Final -> {
                    emptyCount = 0
                    if (!handleIdleUtterance(heard.text)) return
                }
                is SpeechInputResult.Failed -> {
                    if (heard.fatal) {
                        say(phrases.recognizerUnavailable.format(heard.reason))
                        return
                    }
                    // Say why once, so an eyes-closed user knows it is not just silence.
                    if (!lastListenFailed) say(phrases.recognitionFailed.format(heard.reason))
                    emptyCount += 1
                }
                SpeechInputResult.NoMatch -> emptyCount += 1
            }
            lastListenFailed = heard is SpeechInputResult.Failed
            if (emptyCount == 2) {
                say(phrases.stillHere)
            } else if (emptyCount >= 3) {
                say(phrases.standby)
                publishPhase(VoiceCallPhase.Standby)
                wakeSignal.receive()
                emptyCount = 0
            }
        }
    }

    /** Returns false when the call should end. */
    private suspend fun handleIdleUtterance(text: String): Boolean {
        return when (val command = VoiceCommandParser.parse(text)) {
            VoiceCommand.HangUp -> {
                say(phrases.hangUp)
                false
            }
            VoiceCommand.Repeat -> {
                say(lastSpoken.ifBlank { phrases.nothingToRepeat })
                true
            }
            VoiceCommand.Interrupt -> {
                if (VoiceTurn.isActive(detail)) interrupt() else say(phrases.nothingRunning)
                true
            }
            VoiceCommand.Approve, VoiceCommand.Reject -> {
                val notice = VoiceTurn.pendingApproval(detail, emptySet())
                if (notice != null) {
                    respond(notice, approve = command == VoiceCommand.Approve)
                    awaitTurn(VoiceTurn.baseline(detail), sawActiveInitially = true)
                } else {
                    sendAndAwait(text)
                }
            }
            is VoiceCommand.Message -> if (command.text.isBlank()) true else sendAndAwait(command.text)
        }
    }

    private suspend fun sendAndAwait(text: String): Boolean {
        publishPhase(VoiceCallPhase.Sending)
        val baseline = VoiceTurn.baseline(detail)
        val result = controller.sendMessage(
            sessionId = sessionId,
            content = phrases.messagePrefix + text,
            clientMessageId = "opt_${UUID.randomUUID()}",
        )
        result.exceptionOrNull()?.let { error ->
            say(phrases.sendFailed.format(error.message.orEmpty()))
            return true
        }
        cues.acknowledged()
        return awaitTurn(baseline, sawActiveInitially = false)
    }

    /** Waits for the agent to finish, handling approvals and headset wake-ups. */
    private suspend fun awaitTurn(baseline: Int, sawActiveInitially: Boolean): Boolean {
        var sawActive = sawActiveInitially
        var ticks = 0
        publishPhase(VoiceCallPhase.AgentWorking)
        while (true) {
            val woke = withTimeoutOrNull(POLL_INTERVAL_MS) { wakeSignal.receive() } != null
            if (VoiceTurn.isActive(detail)) sawActive = true

            val notice = VoiceTurn.pendingApproval(detail, handledNotices)
            if (notice != null) {
                if (!handleApproval(notice)) return false
                publishPhase(VoiceCallPhase.AgentWorking)
                continue
            }
            if (woke) {
                if (!handleWhileRunning()) return false
                publishPhase(VoiceCallPhase.AgentWorking)
                continue
            }
            if (VoiceTurn.isTurnFinished(detail, baseline, sawActive)) {
                speakReply(baseline)
                return true
            }
            ticks += 1
            // The session socket does not always deliver the final idle state (the session
            // screen only caught it after reconnecting), so pull runtime state while waiting.
            if (ticks % RUNTIME_REFRESH_EVERY_TICKS == 0) refreshRuntimeState()
            if (!sawActive && ticks * POLL_INTERVAL_MS >= NOT_STARTED_TIMEOUT_MS) {
                say(phrases.notStarted)
                return true
            }
            if (ticks % WORKING_CUE_EVERY_TICKS == 0) cues.working()
        }
    }

    private suspend fun refreshRuntimeState() {
        val requestState = detail
        val refreshed = runCatching { controller.refreshRuntimeLiveDomains(sessionId, requestState) }
            .onFailure { error -> Log.w(TAG, "runtime refresh failed: ${error.message}") }
            .getOrNull() ?: return
        updateDetail("refresh") { current -> controller.mergeRuntimeLiveState(current, requestState, refreshed) }
    }

    /** Applies [transform] to [detail] and logs runtime status changes with their source. */
    private fun updateDetail(source: String, transform: (SessionDetailState) -> SessionDetailState) {
        val before = VoiceTurn.status(detail)
        detail = transform(detail)
        val after = VoiceTurn.status(detail)
        if (after != before) {
            Log.i(TAG, "runtime status $before -> $after via $source (updatedSeq=${detail.runtime.updatedSeq})")
        }
    }

    private suspend fun handleWhileRunning(): Boolean {
        val heard = listen() as? SpeechInputResult.Final ?: return true
        when (val command = VoiceCommandParser.parse(heard.text)) {
            VoiceCommand.HangUp -> {
                say(phrases.hangUp)
                return false
            }
            VoiceCommand.Interrupt -> interrupt()
            VoiceCommand.Repeat -> say(lastSpoken.ifBlank { phrases.nothingToRepeat })
            VoiceCommand.Approve, VoiceCommand.Reject -> {
                val notice = VoiceTurn.pendingApproval(detail, emptySet())
                if (notice != null) respond(notice, approve = command == VoiceCommand.Approve) else say(phrases.steerUnavailable)
            }
            // Messages are not sent while a turn runs (no steer path, same as Web/Desktop).
            is VoiceCommand.Message -> if (command.text.isNotBlank()) say(phrases.steerUnavailable)
        }
        return true
    }

    private suspend fun handleApproval(notice: RuntimeNotice): Boolean {
        handledNotices += notice.noticeId
        publishPhase(VoiceCallPhase.AwaitingApproval)
        val body = listOfNotNull(notice.title, notice.message?.take(MAX_NOTICE_CHARS))
            .map(String::trim)
            .filter(String::isNotBlank)
            .distinct()
            .joinToString("。")
        if (!VoiceTurn.canAnswerByVoice(notice)) {
            say(phrases.approvalNeedsScreen.format(body))
            return true
        }
        say(phrases.approvalPrompt.format(body))
        repeat(APPROVAL_ATTEMPTS) {
            when (val heard = listen()) {
                is SpeechInputResult.Final -> when (VoiceCommandParser.parse(heard.text)) {
                    VoiceCommand.Approve -> {
                        respond(notice, approve = true)
                        return true
                    }
                    VoiceCommand.Reject -> {
                        respond(notice, approve = false)
                        return true
                    }
                    VoiceCommand.HangUp -> {
                        say(phrases.hangUp)
                        return false
                    }
                    VoiceCommand.Interrupt -> {
                        interrupt()
                        return true
                    }
                    VoiceCommand.Repeat -> say(phrases.approvalPrompt.format(body))
                    is VoiceCommand.Message -> say(phrases.approvalAnswerHint)
                }
                is SpeechInputResult.Failed -> if (heard.fatal) return true else say(phrases.approvalAnswerHint)
                SpeechInputResult.NoMatch -> say(phrases.approvalAnswerHint)
            }
        }
        say(phrases.approvalNeedsScreen.format(body))
        return true
    }

    private suspend fun respond(notice: RuntimeNotice, approve: Boolean) {
        val action = VoiceTurn.approvalAction(notice, approve)
        if (action == null) {
            say(phrases.approvalNeedsScreen.format(notice.title))
            return
        }
        controller.respondNotice(sessionId, notice.noticeId, action.actionId, null)
            .onSuccess {
                cues.acknowledged()
                say(phrases.approvalDone.format(action.label.ifBlank { action.actionId }))
            }
            .onFailure { error -> say(phrases.actionFailed.format(error.message.orEmpty())) }
    }

    private suspend fun interrupt() {
        if (!capabilityUsable(SESSION_INTERRUPT_CAPABILITY)) {
            say(phrases.actionFailed.format(""))
            return
        }
        controller.interrupt(sessionId)
            .onSuccess { say(phrases.interrupted) }
            .onFailure { error -> say(phrases.actionFailed.format(error.message.orEmpty())) }
    }

    private suspend fun speakReply(baseline: Int) {
        val parts = mutableListOf<String>()
        if (VoiceTurn.status(detail) == SessionRuntimeStatus.Error) {
            parts += phrases.turnFailed.format(detail.runtime.statusReason.orEmpty())
        }
        val reply = SpeechTextSanitizer.sanitize(VoiceTurn.replyText(detail.messages, baseline), phrases.labels)
        if (reply.isNotBlank()) parts += reply
        val work = VoiceTurn.workSummary(detail.messages, baseline)
        if (work.changedFiles > 0) parts += phrases.workSummaryFiles.format(work.changedFiles)
        if (work.commands > 0) parts += phrases.workSummaryCommands.format(work.commands)
        if (parts.isEmpty()) parts += phrases.noReply
        say(parts.joinToString("\n"))
    }

    private suspend fun listen(): SpeechInputResult {
        while (wakeSignal.tryReceive().isSuccess) Unit
        publishPhase(VoiceCallPhase.Listening)
        VoiceCallRegistry.update { it.copy(partialTranscript = "", listeningHint = "") }
        cues.listening()
        delay(CUE_SETTLE_MS)
        val result = input.listen(
            onPartial = { partial -> VoiceCallRegistry.update { it.copy(partialTranscript = partial) } },
            onHint = { hint -> VoiceCallRegistry.update { it.copy(listeningHint = hint) } },
        )
        Log.i(TAG, "listen result: ${result.describe()}")
        VoiceCallRegistry.update { state ->
            when (result) {
                is SpeechInputResult.Final -> state.copy(
                    partialTranscript = "",
                    listeningHint = "",
                    lastHeard = result.text,
                    errorMessage = null,
                )
                SpeechInputResult.NoMatch -> state.copy(listeningHint = "", errorMessage = phrases.noSpeechHeard)
                is SpeechInputResult.Failed -> state.copy(
                    listeningHint = "",
                    errorMessage = phrases.recognitionFailed.format(result.reason),
                )
            }
        }
        return result
    }

    /** Speaks [text]; a wake signal (headset button) skips the rest of it. */
    private suspend fun say(text: String) {
        if (text.isBlank()) return
        lastSpoken = text
        publishPhase(VoiceCallPhase.Speaking)
        VoiceCallRegistry.update { it.copy(lastSpoken = text) }
        coroutineScope {
            val skipper = launch {
                wakeSignal.receive()
                output.stop()
            }
            output.speak(text)
            skipper.cancel()
        }
    }

    private fun publishPhase(phase: VoiceCallPhase) {
        VoiceCallRegistry.update { if (it.phase == phase) it else it.copy(phase = phase) }
    }

    private fun capabilityUsable(capability: String): Boolean {
        val runtimeId = detail.session?.runtimeId ?: detail.runtime.runtimeId
        val runtimeType = detail.session?.runtimeType ?: detail.runtime.runtimeType
        return detail.capabilities.isUsable(capability, runtimeId, runtimeType)
    }

    private fun lastUserOrderSeq(): Int =
        detail.messages.filter { it.author == MessageAuthor.User }.maxOfOrNull { it.orderSeq } ?: 0

    private fun startRealtime(scope: CoroutineScope): Job = realtime.start(
        scope = scope,
        sessionId = sessionId,
        cursor = { withContext(Dispatchers.Main.immediate) { detail.realtime.cursor } },
        onEvents = { events ->
            withContext(Dispatchers.Main.immediate) {
                updateDetail("event ${events.map { it.type }.distinct()}") { current ->
                    controller.applyRealtimeEvents(current, events, emptyList())
                }
            }
        },
        onCursorAdvanced = { cursor ->
            withContext(Dispatchers.Main.immediate) {
                detail = detail.copy(
                    realtime = detail.realtime.copy(cursor = laterEventCursor(detail.realtime.cursor, cursor)),
                )
            }
        },
        onSnapshotRequired = { _ ->
            val current = withContext(Dispatchers.Main.immediate) { detail }
            controller.loadInitialSnapshot(sessionId, emptyList(), current).onSuccess { loaded ->
                withContext(Dispatchers.Main.immediate) {
                    updateDetail("snapshot") { current ->
                        controller.mergeSnapshotWithLiveState(sessionId, loaded, current).completeSnapshotLoad()
                    }
                }
            }
        },
        onRuntimeRefreshRequired = { connectionGeneration, refreshGeneration ->
            val requestState = withContext(Dispatchers.Main.immediate) { detail }
            val refreshed = controller.refreshRuntimeLiveDomains(sessionId, requestState)
            withContext(Dispatchers.Main.immediate) {
                if (realtime.isCurrentRuntimeRefresh(connectionGeneration, refreshGeneration)) {
                    updateDetail("socket refresh") { current ->
                        controller.mergeRuntimeLiveState(current, requestState, refreshed)
                    }
                }
            }
        },
        onConnectionChanged = { connected, _, _, _ ->
            VoiceCallRegistry.update { it.copy(connected = connected) }
        },
    )

    private companion object {
        const val TAG = "AAVoice"
        const val POLL_INTERVAL_MS = 1_000L
        const val CUE_SETTLE_MS = 200L
        const val WORKING_CUE_EVERY_TICKS = 8
        const val RUNTIME_REFRESH_EVERY_TICKS = 5
        const val NOT_STARTED_TIMEOUT_MS = 30_000L
        const val APPROVAL_ATTEMPTS = 3
        const val MAX_NOTICE_CHARS = 200
    }
}

private fun SpeechInputResult.describe(): String = when (this) {
    is SpeechInputResult.Final -> "final textLength=${text.length}"
    SpeechInputResult.NoMatch -> "no match"
    is SpeechInputResult.Failed -> "failed fatal=$fatal reason=$reason"
}
