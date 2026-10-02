package com.agentsanywhere.app.ui.screens.voice

import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.agentsanywhere.app.R
import com.agentsanywhere.app.feature.voice.VoiceCallPhase
import com.agentsanywhere.app.feature.voice.VoiceCallRegistry
import com.agentsanywhere.app.feature.voice.VoiceCallUiState
import com.agentsanywhere.app.feature.voice.VoiceRecognitionMode
import com.agentsanywhere.app.ui.designsystem.LocalAAColors
import com.agentsanywhere.app.ui.designsystem.noRippleClickable
import com.composables.icons.lucide.Lucide
import com.composables.icons.lucide.Mic
import com.composables.icons.lucide.PhoneOff

/** Full-screen call view shown over the session while a voice call is active. */
@Composable
fun VoiceCallScreen(
    state: VoiceCallUiState,
    onTalk: () -> Unit,
    onHangUp: () -> Unit,
    onSelectRecognition: (VoiceRecognitionMode) -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = LocalAAColors.current
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    var canShowBubble by remember { mutableStateOf(Settings.canDrawOverlays(context)) }
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_START -> {
                    VoiceCallRegistry.setCallScreenVisible(true)
                    canShowBubble = Settings.canDrawOverlays(context)
                }
                Lifecycle.Event.ON_STOP -> VoiceCallRegistry.setCallScreenVisible(false)
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        if (lifecycleOwner.lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
            VoiceCallRegistry.setCallScreenVisible(true)
        }
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            VoiceCallRegistry.setCallScreenVisible(false)
        }
    }
    Box(
        modifier = modifier
            .fillMaxSize()
            .background(colors.canvas)
            .noRippleClickable(onClick = {})
            .systemBarsPadding()
            .padding(horizontal = 28.dp, vertical = 24.dp),
    ) {
        Column(
            modifier = Modifier.fillMaxSize(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Spacer(Modifier.height(36.dp))
            Text(
                text = state.title,
                color = colors.muted,
                fontSize = 15.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(14.dp))
            Text(
                text = phaseLabel(state.phase),
                color = colors.ink,
                fontSize = 28.sp,
                fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.Center,
            )
            if (state.phase == VoiceCallPhase.Listening && state.listeningHint.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(state.listeningHint, color = colors.muted, fontSize = 14.sp, textAlign = TextAlign.Center)
            }
            if (!state.connected && state.phase != VoiceCallPhase.Connecting) {
                Spacer(Modifier.height(6.dp))
                Text(stringResource(R.string.voice_call_reconnecting), color = Color(0xFFF59E0B), fontSize = 13.sp)
            }
            Spacer(Modifier.height(28.dp))
            Column(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(18.dp),
            ) {
                val heard = state.partialTranscript.ifBlank { state.lastHeard }
                if (heard.isNotBlank()) {
                    TranscriptBlock(label = stringResource(R.string.voice_call_you_said), text = heard)
                }
                if (state.lastSpoken.isNotBlank()) {
                    TranscriptBlock(label = stringResource(R.string.voice_call_agent_said), text = state.lastSpoken)
                }
                state.errorMessage?.takeIf(String::isNotBlank)?.let { error ->
                    Text(error, color = Color(0xFFEF4444), fontSize = 13.sp)
                }
            }
            if (!canShowBubble) {
                BubblePermissionHint(
                    onEnable = {
                        context.startActivity(
                            Intent(
                                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                Uri.parse("package:${context.packageName}"),
                            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                        )
                    },
                )
            }
            RecognitionSwitch(state = state, onSelect = onSelectRecognition)
            Text(
                text = stringResource(R.string.voice_call_commands_hint),
                color = colors.muted,
                fontSize = 12.sp,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(vertical = 18.dp),
            )
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                CallButton(
                    icon = Lucide.Mic,
                    label = stringResource(R.string.voice_call_talk),
                    surface = colors.raisedSurface,
                    tint = colors.ink,
                    onClick = onTalk,
                )
                CallButton(
                    icon = Lucide.PhoneOff,
                    label = stringResource(R.string.voice_call_hang_up),
                    surface = Color(0xFFEF4444),
                    tint = Color.White,
                    onClick = onHangUp,
                )
            }
        }
    }
}

@Composable
private fun BubblePermissionHint(onEnable: () -> Unit) {
    val colors = LocalAAColors.current
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 12.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(colors.raisedSurface)
            .padding(horizontal = 14.dp, vertical = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = stringResource(R.string.voice_call_bubble_permission_hint),
            color = colors.muted,
            fontSize = 12.sp,
            modifier = Modifier.weight(1f),
        )
        Text(
            text = stringResource(R.string.voice_call_bubble_permission_enable),
            color = colors.ink,
            fontSize = 13.sp,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.noRippleClickable(onClick = onEnable),
        )
    }
}

@Composable
private fun RecognitionSwitch(
    state: VoiceCallUiState,
    onSelect: (VoiceRecognitionMode) -> Unit,
) {
    val colors = LocalAAColors.current
    Row(
        modifier = Modifier.padding(top = 18.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(stringResource(R.string.voice_call_recognition_label), color = colors.muted, fontSize = 12.sp)
        listOf(
            Triple(VoiceRecognitionMode.System, R.string.voice_call_recognition_system, state.systemRecognitionAvailable),
            Triple(VoiceRecognitionMode.Server, R.string.voice_call_recognition_server, state.serverRecognitionAvailable),
        ).forEach { (mode, labelRes, enabled) ->
            val selected = state.recognitionMode == mode
            Text(
                text = stringResource(labelRes),
                color = when {
                    selected -> colors.canvas
                    enabled -> colors.ink
                    else -> colors.muted.copy(alpha = 0.5f)
                },
                fontSize = 13.sp,
                fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                modifier = Modifier
                    .clip(CircleShape)
                    .background(if (selected) colors.ink else colors.raisedSurface)
                    .then(if (enabled && !selected) Modifier.noRippleClickable(onClick = { onSelect(mode) }) else Modifier)
                    .padding(horizontal = 14.dp, vertical = 6.dp),
            )
        }
    }
}

@Composable
private fun TranscriptBlock(label: String, text: String) {
    val colors = LocalAAColors.current
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(label, color = colors.muted, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
        Text(
            text = text,
            color = colors.inkSoft,
            fontSize = 17.sp,
            lineHeight = 25.sp,
            modifier = Modifier.heightIn(max = 360.dp),
        )
    }
}

@Composable
private fun CallButton(
    icon: ImageVector,
    label: String,
    surface: Color,
    tint: Color,
    onClick: () -> Unit,
) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Box(
            modifier = Modifier
                .size(72.dp)
                .clip(CircleShape)
                .background(surface)
                .noRippleClickable(onClick = onClick),
            contentAlignment = Alignment.Center,
        ) {
            Icon(icon, contentDescription = label, tint = tint, modifier = Modifier.size(28.dp))
        }
        Text(label, color = LocalAAColors.current.muted, fontSize = 13.sp)
    }
}

@Composable
private fun phaseLabel(phase: VoiceCallPhase): String = stringResource(
    when (phase) {
        VoiceCallPhase.Connecting -> R.string.voice_call_phase_connecting
        VoiceCallPhase.Listening -> R.string.voice_call_phase_listening
        VoiceCallPhase.Sending -> R.string.voice_call_phase_sending
        VoiceCallPhase.AgentWorking -> R.string.voice_call_phase_working
        VoiceCallPhase.AwaitingApproval -> R.string.voice_call_phase_approval
        VoiceCallPhase.Speaking -> R.string.voice_call_phase_speaking
        VoiceCallPhase.Standby -> R.string.voice_call_phase_standby
        VoiceCallPhase.Ended -> R.string.voice_call_phase_ended
    },
)
