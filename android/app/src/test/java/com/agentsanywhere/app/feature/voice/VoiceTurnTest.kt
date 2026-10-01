package com.agentsanywhere.app.feature.voice

import com.agentsanywhere.app.feature.sessiondetail.MessageAuthor
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNotice
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNoticeAction
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNoticeActionInput
import com.agentsanywhere.app.feature.sessiondetail.RuntimeNotices
import com.agentsanywhere.app.feature.sessiondetail.SessionDetailState
import com.agentsanywhere.app.feature.sessiondetail.SessionRuntimeState
import com.agentsanywhere.app.feature.sessiondetail.SessionRuntimeStatus
import com.agentsanywhere.app.feature.sessiondetail.SessionTimelineState
import com.agentsanywhere.app.feature.sessiondetail.TimelineFileChange
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessage
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessageKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceTurnTest {
    private fun message(
        seq: Int,
        author: MessageAuthor,
        text: String = "",
        kind: TimelineMessageKind = TimelineMessageKind.Text,
        fileChanges: List<TimelineFileChange> = emptyList(),
    ) = TimelineMessage(
        id = "item-$seq",
        author = author,
        text = text,
        kind = kind,
        orderSeq = seq,
        fileChanges = fileChanges,
    )

    private fun state(
        status: SessionRuntimeStatus,
        messages: List<TimelineMessage> = emptyList(),
        notices: List<RuntimeNotice> = emptyList(),
    ) = SessionDetailState(
        timeline = SessionTimelineState(messages = messages),
        runtime = SessionRuntimeState(status = status),
        notices = RuntimeNotices(notices = notices),
    )

    private fun action(id: String, style: String = "secondary", inputRequired: Boolean = false) = RuntimeNoticeAction(
        actionId = id,
        label = id,
        style = style,
        input = RuntimeNoticeActionInput(required = inputRequired, schema = null, uiSchema = null),
        unknown = emptyMap(),
    )

    private fun notice(id: String, updatedSeq: Int, actions: List<RuntimeNoticeAction>, status: String = "open") =
        RuntimeNotice(
            noticeId = id,
            type = "interaction",
            sessionId = "s1",
            title = "Run command",
            message = "rm -rf build",
            severity = "warning",
            status = status,
            interactionType = "approval",
            blocking = null,
            responseRequired = true,
            revision = 1,
            updatedSeq = updatedSeq,
            source = emptyMap(),
            actions = actions,
            context = emptyMap(),
            metadata = emptyMap(),
            expiresAt = null,
            createdAt = null,
            updatedAt = null,
            resolvedAt = null,
        )

    @Test
    fun turnIsNotFinishedWhileRunningOrBeforeAnythingHappened() {
        val history = listOf(message(1, MessageAuthor.User, "hi"), message(2, MessageAuthor.Agent, "old reply"))
        assertFalse(VoiceTurn.isTurnFinished(state(SessionRuntimeStatus.Running, history), baseline = 2, sawActive = true))
        assertFalse(VoiceTurn.isTurnFinished(state(SessionRuntimeStatus.Idle, history), baseline = 2, sawActive = false))
        assertFalse(VoiceTurn.isTurnFinished(state(SessionRuntimeStatus.Unknown, history), baseline = 2, sawActive = true))
    }

    @Test
    fun turnFinishesOnIdleAfterActivityOrWithNewReply() {
        val withReply = listOf(
            message(1, MessageAuthor.User, "hi"),
            message(3, MessageAuthor.User, "fix it"),
            message(4, MessageAuthor.Agent, "done"),
        )
        assertTrue(VoiceTurn.isTurnFinished(state(SessionRuntimeStatus.Idle, withReply), baseline = 2, sawActive = false))
        assertTrue(VoiceTurn.isTurnFinished(state(SessionRuntimeStatus.Idle), baseline = 2, sawActive = true))
        assertTrue(VoiceTurn.isTurnFinished(state(SessionRuntimeStatus.Error), baseline = 2, sawActive = true))
    }

    @Test
    fun replyTextOnlyIncludesAgentTextAfterBaseline() {
        val messages = listOf(
            message(1, MessageAuthor.Agent, "old"),
            message(3, MessageAuthor.User, "question"),
            message(4, MessageAuthor.Agent, "thinking", kind = TimelineMessageKind.Reasoning),
            message(5, MessageAuthor.Agent, "first"),
            message(6, MessageAuthor.Tool, "ls", kind = TimelineMessageKind.Command),
            message(7, MessageAuthor.Agent, "second"),
        )
        assertEquals("first\n\nsecond", VoiceTurn.replyText(messages, baseline = 2))
    }

    @Test
    fun workSummaryCountsDistinctFilesAndCommands() {
        val messages = listOf(
            message(1, MessageAuthor.Tool, kind = TimelineMessageKind.FileChange, fileChanges = listOf(TimelineFileChange("edit", "old.kt", ""))),
            message(3, MessageAuthor.Tool, kind = TimelineMessageKind.FileChange, fileChanges = listOf(
                TimelineFileChange("edit", "a.kt", ""),
                TimelineFileChange("edit", "b.kt", ""),
            )),
            message(4, MessageAuthor.Tool, kind = TimelineMessageKind.FileChange, fileChanges = listOf(TimelineFileChange("edit", "a.kt", ""))),
            message(5, MessageAuthor.Tool, kind = TimelineMessageKind.Command),
        )
        assertEquals(VoiceWorkSummary(changedFiles = 2, commands = 1), VoiceTurn.workSummary(messages, baseline = 2))
    }

    @Test
    fun pendingApprovalPicksOldestUnhandledRespondableNotice() {
        val actions = listOf(action("approve", "primary"), action("reject", "danger"))
        val notices = listOf(
            notice("n2", updatedSeq = 9, actions = actions),
            notice("n1", updatedSeq = 5, actions = actions),
            notice("n0", updatedSeq = 1, actions = actions, status = "resolved"),
        )
        val current = state(SessionRuntimeStatus.WaitingApproval, notices = notices)
        assertEquals("n1", VoiceTurn.pendingApproval(current, emptySet())?.noticeId)
        assertEquals("n2", VoiceTurn.pendingApproval(current, setOf("n1"))?.noticeId)
        assertNull(VoiceTurn.pendingApproval(current, setOf("n1", "n2")))
    }

    @Test
    fun approvalActionPrefersKnownIdsThenStyle() {
        val standard = notice("n", 1, listOf(action("approve_for_session"), action("approve", "primary"), action("reject", "danger")))
        assertEquals("approve", VoiceTurn.approvalAction(standard, approve = true)?.actionId)
        assertEquals("reject", VoiceTurn.approvalAction(standard, approve = false)?.actionId)

        val styled = notice("n", 1, listOf(action("yes_please", "primary"), action("nope", "danger")))
        assertEquals("yes_please", VoiceTurn.approvalAction(styled, approve = true)?.actionId)
        assertEquals("nope", VoiceTurn.approvalAction(styled, approve = false)?.actionId)
    }

    @Test
    fun noticesNeedingInputCannotBeAnsweredByVoice() {
        val needsInput = notice("n", 1, listOf(action("submit", "primary", inputRequired = true)))
        assertFalse(VoiceTurn.canAnswerByVoice(needsInput))
    }
}
