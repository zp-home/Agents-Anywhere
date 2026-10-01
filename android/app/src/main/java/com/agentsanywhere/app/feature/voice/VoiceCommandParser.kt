package com.agentsanywhere.app.feature.voice

/**
 * Spoken control phrases recognised during a voice call.
 *
 * A phrase only counts as a command when the whole utterance (after stripping
 * punctuation and filler words) is the phrase itself, so "停止那个服务" is still
 * sent to the agent as a normal instruction.
 */
sealed interface VoiceCommand {
    data object Interrupt : VoiceCommand
    data object Approve : VoiceCommand
    data object Reject : VoiceCommand
    data object Repeat : VoiceCommand
    data object HangUp : VoiceCommand
    data class Message(val text: String) : VoiceCommand
}

object VoiceCommandParser {
    private val interruptPhrases = setOf(
        "停止", "停下", "停", "打断", "暂停", "别做", "停止运行", "中断",
        "stop", "interrupt", "cancel",
    )
    private val approvePhrases = setOf(
        "同意", "批准", "可以", "允许", "确认", "好", "好的", "行", "执行", "继续",
        "approve", "yes", "allow", "ok", "okay",
    )
    private val rejectPhrases = setOf(
        "拒绝", "不行", "不同意", "不可以", "不允许", "不要", "取消", "否",
        "reject", "deny", "no",
    )
    private val repeatPhrases = setOf(
        "重复", "再说一遍", "再说一次", "重复一遍", "没听清", "再念一遍",
        "repeat", "say again",
    )
    private val hangUpPhrases = setOf(
        "挂断", "挂", "结束通话", "挂电话", "退出通话", "结束",
        "hang up", "end call", "bye",
    )

    private val fillerPrefixes = listOf("嗯", "啊", "呃", "额", "那个", "请", "麻烦", "帮我")
    private val fillerSuffixes = listOf("吧", "啊", "呀", "了", "吗", "一下", "嘛", "哦", "呢")
    private val punctuation = Regex("[\\p{P}\\p{S}\\s]+")

    fun parse(raw: String): VoiceCommand {
        val text = raw.trim()
        val normalized = normalize(text)
        return when {
            normalized.isEmpty() -> VoiceCommand.Message("")
            normalized in hangUpPhrases -> VoiceCommand.HangUp
            normalized in interruptPhrases -> VoiceCommand.Interrupt
            normalized in rejectPhrases -> VoiceCommand.Reject
            normalized in approvePhrases -> VoiceCommand.Approve
            normalized in repeatPhrases -> VoiceCommand.Repeat
            else -> VoiceCommand.Message(text)
        }
    }

    internal fun normalize(raw: String): String {
        var value = raw.lowercase().replace(punctuation, " ").trim().replace(Regex(" +"), " ")
        var changed = true
        while (changed && value.isNotEmpty()) {
            changed = false
            fillerPrefixes.firstOrNull { value.startsWith(it) && value.length > it.length }?.let {
                value = value.removePrefix(it).trim()
                changed = true
            }
            fillerSuffixes.firstOrNull { value.endsWith(it) && value.length > it.length }?.let {
                value = value.removeSuffix(it).trim()
                changed = true
            }
        }
        return value
    }
}
