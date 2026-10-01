package com.agentsanywhere.app.feature.voice

import com.agentsanywhere.app.feature.sessiondetail.MessageAuthor
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNotice
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNoticeAction
import com.agentsanywhere.app.feature.sessiondetail.SessionDetailState
import com.agentsanywhere.app.feature.sessiondetail.SessionRuntimeStatus
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessage
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessageKind
import com.agentsanywhere.app.model.SessionStatus

/** Pure decisions the voice call engine makes from a [SessionDetailState]. */
object VoiceTurn {
    private val activeStatuses = setOf(
        SessionRuntimeStatus.Waiting,
        SessionRuntimeStatus.Pending,
        SessionRuntimeStatus.Running,
        SessionRuntimeStatus.Stopping,
        SessionRuntimeStatus.WaitingApproval,
        SessionRuntimeStatus.Blocked,
    )
    private val approveActionIds = listOf("approve", "allow", "accept", "continue", "submit")
    private val rejectActionIds = listOf("reject", "deny", "decline", "cancel", "dismiss")

    fun status(state: SessionDetailState): SessionRuntimeStatus {
        if (state.runtime.status != SessionRuntimeStatus.Unknown) return state.runtime.status
        return when (state.session?.status) {
            SessionStatus.Idle -> SessionRuntimeStatus.Idle
            SessionStatus.Waiting -> SessionRuntimeStatus.Waiting
            SessionStatus.Pending -> SessionRuntimeStatus.Pending
            SessionStatus.Running -> SessionRuntimeStatus.Running
            SessionStatus.Stopping -> SessionRuntimeStatus.Stopping
            SessionStatus.WaitingApproval -> SessionRuntimeStatus.WaitingApproval
            SessionStatus.Blocked -> SessionRuntimeStatus.Blocked
            SessionStatus.Error -> SessionRuntimeStatus.Error
            SessionStatus.Unknown, null -> SessionRuntimeStatus.Unknown
        }
    }

    fun isActive(state: SessionDetailState): Boolean = status(state) in activeStatuses

    /** Highest timeline position seen so far; replies are the agent items after it. */
    fun baseline(state: SessionDetailState): Int = state.messages.maxOfOrNull { it.orderSeq } ?: 0

    /**
     * The turn is over once the runtime is back to idle/error and we either saw it
     * running or already have an agent reply newer than [baseline].
     */
    fun isTurnFinished(state: SessionDetailState, baseline: Int, sawActive: Boolean): Boolean {
        val status = status(state)
        if (status in activeStatuses || status == SessionRuntimeStatus.Unknown) return false
        if (state.sending) return false
        if (state.messages.any { it.optimistic && it.status == "running" }) return false
        return sawActive || replyMessages(state.messages, baseline).isNotEmpty()
    }

    fun replyMessages(messages: List<TimelineMessage>, baseline: Int): List<TimelineMessage> =
        messages
            .filter { it.orderSeq > baseline && it.author == MessageAuthor.Agent && it.kind == TimelineMessageKind.Text }
            .filter { it.text.isNotBlank() }
            .sortedBy { it.orderSeq }

    fun replyText(messages: List<TimelineMessage>, baseline: Int): String =
        replyMessages(messages, baseline).joinToString("\n\n") { it.text.trim() }

    /** Number of changed files and executed commands after [baseline]. */
    fun workSummary(messages: List<TimelineMessage>, baseline: Int): VoiceWorkSummary {
        val turn = messages.filter { it.orderSeq > baseline }
        val files = turn
            .filter { it.kind == TimelineMessageKind.FileChange }
            .flatMap { message -> message.fileChanges.map { it.path }.ifEmpty { listOf(message.id) } }
            .toSet()
        val commands = turn.count { it.kind == TimelineMessageKind.Command }
        return VoiceWorkSummary(changedFiles = files.size, commands = commands)
    }

    fun pendingApproval(state: SessionDetailState, handled: Set<String>): RuntimeNotice? =
        state.notices.notices
            .filter { it.respondable && it.noticeId !in handled }
            .minWithOrNull(compareBy<RuntimeNotice> { it.updatedSeq }.thenBy { it.noticeId })

    /** Action spoken "同意/拒绝" maps to; null when the notice needs on-screen input. */
    fun approvalAction(notice: RuntimeNotice, approve: Boolean): RuntimeNoticeAction? {
        val usable = notice.actions.filter { it.actionId.isNotBlank() && !it.input.required }
        val ids = if (approve) approveActionIds else rejectActionIds
        ids.forEach { id -> usable.firstOrNull { it.actionId == id }?.let { return it } }
        val style = if (approve) "primary" else "danger"
        return usable.firstOrNull { it.style == style }
    }

    fun canAnswerByVoice(notice: RuntimeNotice): Boolean = approvalAction(notice, approve = true) != null
}

data class VoiceWorkSummary(
    val changedFiles: Int,
    val commands: Int,
)
