package com.agentsanywhere.app.feature.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.speech.SpeechRecognizer
import android.view.KeyEvent
import com.agentsanywhere.app.R
import com.agentsanywhere.app.api.ApiClient
import com.agentsanywhere.app.api.RealtimeApi
import com.agentsanywhere.app.api.SessionsApi
import com.agentsanywhere.app.api.SpeechApi
import com.agentsanywhere.app.feature.auth.AuthSessionStore
import com.agentsanywhere.app.feature.realtime.RealtimeClientIdStore
import com.agentsanywhere.app.feature.realtime.SessionRealtimeController
import com.agentsanywhere.app.feature.sessiondetail.SessionDetailController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

/**
 * Foreground service that owns a voice call so it keeps listening, receiving session
 * events and speaking while the screen is off.
 */
class VoiceCallService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var engine: VoiceCallEngine? = null
    private var activeSessionId: String? = null
    private var input: SpeechInput? = null
    private var output: SpeechOutput? = null
    private var cues: VoiceCues? = null
    private var mediaSession: MediaSession? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var focusRequest: AudioFocusRequest? = null
    private var switchableInput: SwitchableSpeechInput? = null
    private var setupJob: Job? = null
    private var bubble: VoiceCallBubble? = null
    private var bubbleJob: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val sessionId = intent.getStringExtra(EXTRA_SESSION_ID)
                val title = intent.getStringExtra(EXTRA_TITLE).orEmpty()
                if (sessionId.isNullOrBlank() || !enterForeground(title)) {
                    endCall()
                } else {
                    startCall(sessionId, title)
                }
            }
            ACTION_TALK -> engine?.wake()
            ACTION_SET_RECOGNITION -> setRecognition(
                VoiceRecognitionMode.fromStorage(intent.getStringExtra(EXTRA_RECOGNITION)),
            )
            ACTION_HANG_UP -> endCall()
            else -> if (engine == null) stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        tearDown()
        VoiceCallRegistry.publish(null)
        scope.cancel()
        super.onDestroy()
    }

    private fun startCall(sessionId: String, title: String) {
        if (activeSessionId == sessionId) return
        tearDown()
        activeSessionId = sessionId
        val preferences = VoiceRecognitionPreferences(this)
        val systemAvailable = SpeechRecognizer.isRecognitionAvailable(this)
        VoiceCallRegistry.publish(
            VoiceCallUiState(
                sessionId = sessionId,
                title = title,
                recognitionMode = preferences.read(),
                systemRecognitionAvailable = systemAvailable,
            ),
        )

        val locale = Locale.getDefault()
        val sessionStore = AuthSessionStore(this)
        val apiClient = ApiClient()
        val speechApi = SpeechApi(apiClient)
        val speechOutput = TtsSpeechOutput(this, locale) { problem ->
            VoiceCallRegistry.update { it.copy(errorMessage = getString(R.string.voice_call_tts_problem, problem)) }
        }.also { output = it }
        val voiceCues = ToneVoiceCues().also { cues = it }
        acquireWakeLock()
        requestAudioFocus()
        startMediaSession()
        startBubble()

        setupJob = scope.launch {
            val serverAvailable = withContext(Dispatchers.IO) {
                runCatching {
                    speechApi.getStatus(sessionStore.readServerUrl(), sessionStore.readAccessToken()).available
                }.getOrDefault(false)
            }
            val mode = when {
                !serverAvailable -> VoiceRecognitionMode.System
                !systemAvailable -> VoiceRecognitionMode.Server
                else -> preferences.read()
            }
            val speechInput = SwitchableSpeechInput(
                system = SystemSpeechInput(this@VoiceCallService, locale),
                server = ServerSpeechInput(
                    speechApi = speechApi,
                    sessionStore = sessionStore,
                    labels = ServerSpeechLabels(
                        listening = getString(R.string.voice_call_listening_hint),
                        recording = getString(R.string.voice_call_recording_hint),
                        recognizing = getString(R.string.voice_call_recognizing),
                    ),
                ),
                initial = mode,
            )
            input = speechInput
            switchableInput = speechInput
            VoiceCallRegistry.update {
                it.copy(
                    recognitionMode = mode,
                    serverRecognitionAvailable = serverAvailable,
                    errorMessage = if (!systemAvailable && !serverAvailable) {
                        getString(R.string.voice_call_recognizer_missing)
                    } else {
                        it.errorMessage
                    },
                )
            }
            engine = VoiceCallEngine(
                sessionId = sessionId,
                controller = SessionDetailController(SessionsApi(apiClient), sessionStore),
                realtime = SessionRealtimeController(
                    RealtimeApi(client = apiClient),
                    sessionStore,
                    RealtimeClientIdStore(this@VoiceCallService).readOrCreate(),
                ),
                input = speechInput,
                output = speechOutput,
                cues = voiceCues,
                phrases = voicePhrases(this@VoiceCallService),
                onEnded = ::endCall,
            ).also { it.start(scope) }
        }
    }

    private fun setRecognition(mode: VoiceRecognitionMode) {
        val state = VoiceCallRegistry.state.value ?: return
        val usable = when (mode) {
            VoiceRecognitionMode.System -> state.systemRecognitionAvailable
            VoiceRecognitionMode.Server -> state.serverRecognitionAvailable
        }
        if (!usable) return
        VoiceRecognitionPreferences(this).write(mode)
        switchableInput?.mode = mode
        VoiceCallRegistry.update { it.copy(recognitionMode = mode) }
    }

    private fun endCall() {
        tearDown()
        VoiceCallRegistry.publish(null)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun tearDown() {
        setupJob?.cancel()
        setupJob = null
        bubbleJob?.cancel()
        bubbleJob = null
        bubble?.hide()
        bubble = null
        engine?.stop()
        engine = null
        switchableInput = null
        activeSessionId = null
        input?.release()
        input = null
        output?.release()
        output = null
        cues?.release()
        cues = null
        mediaSession?.run {
            isActive = false
            release()
        }
        mediaSession = null
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        focusRequest?.let { getSystemService(AudioManager::class.java).abandonAudioFocusRequest(it) }
        focusRequest = null
    }

    private fun enterForeground(title: String): Boolean {
        createNotificationChannel()
        val notification = buildNotification(title)
        return runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
        }.isSuccess
    }

    private fun acquireWakeLock() {
        // Keeps timers and the session socket alive while the screen is off and the agent works.
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AgentsAnywhere:VoiceCall")
            .apply {
                setReferenceCounted(false)
                acquire(MAX_CALL_MS)
            }
    }

    private fun requestAudioFocus() {
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .build()
        getSystemService(AudioManager::class.java).requestAudioFocus(request)
        focusRequest = request
    }

    private fun startBubble() {
        val floating = VoiceCallBubble(this, onTap = ::returnToCall).also { bubble = it }
        bubbleJob = scope.launch {
            combine(VoiceCallRegistry.state, VoiceCallRegistry.callScreenVisible) { state, visible ->
                state?.takeIf { !visible && it.phase != VoiceCallPhase.Ended }?.let { bubbleLabel(it.phase) }
            }.distinctUntilChanged().collect { label ->
                if (label == null) floating.hide() else floating.show(label)
            }
        }
    }

    private fun bubbleLabel(phase: VoiceCallPhase): String = getString(
        when (phase) {
            VoiceCallPhase.Connecting -> R.string.voice_call_phase_connecting
            VoiceCallPhase.Listening -> R.string.voice_call_phase_listening
            VoiceCallPhase.Sending -> R.string.voice_call_phase_sending
            VoiceCallPhase.AgentWorking -> R.string.voice_call_phase_working
            VoiceCallPhase.AwaitingApproval -> R.string.voice_call_phase_approval
            VoiceCallPhase.Speaking -> R.string.voice_call_phase_speaking
            VoiceCallPhase.Standby, VoiceCallPhase.Ended -> R.string.voice_call_bubble_paused
        },
    )

    /** Bubble tap: bring the app forward and ask the session screen to show the call view. */
    private fun returnToCall() {
        VoiceCallRegistry.requestShowCall()
        packageManager.getLaunchIntentForPackage(packageName)?.let { launch ->
            launch.addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or Intent.FLAG_ACTIVITY_SINGLE_TOP,
            )
            runCatching { startActivity(launch) }
        }
    }

    private fun startMediaSession() {
        mediaSession = MediaSession(this, "AgentsAnywhereVoiceCall").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onMediaButtonEvent(mediaButtonIntent: Intent): Boolean {
                    val event = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        mediaButtonIntent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent::class.java)
                    } else {
                        @Suppress("DEPRECATION")
                        mediaButtonIntent.getParcelableExtra(Intent.EXTRA_KEY_EVENT)
                    }
                    if (event?.action == KeyEvent.ACTION_DOWN && event.keyCode in HEADSET_KEYS) {
                        engine?.wake()
                        return true
                    }
                    return super.onMediaButtonEvent(mediaButtonIntent)
                }
            })
            setPlaybackState(
                PlaybackState.Builder()
                    .setActions(PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE)
                    .setState(PlaybackState.STATE_PLAYING, 0L, 1f)
                    .build(),
            )
            isActive = true
        }
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.voice_call_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply { setShowBadge(false) }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(title: String): Notification {
        val openApp = packageManager.getLaunchIntentForPackage(packageName)?.let { launch ->
            PendingIntent.getActivity(this, 0, launch, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        }
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(getString(R.string.voice_call_notification_title))
            .setContentText(title.ifBlank { getString(R.string.voice_call_notification_text) })
            .setCategory(Notification.CATEGORY_SERVICE)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openApp)
            .addAction(serviceAction(ACTION_TALK, R.string.voice_call_talk, REQUEST_TALK))
            .addAction(serviceAction(ACTION_HANG_UP, R.string.voice_call_hang_up, REQUEST_HANG_UP))
            .build()
    }

    private fun serviceAction(action: String, labelRes: Int, requestCode: Int): Notification.Action {
        val intent = Intent(this, VoiceCallService::class.java).setAction(action)
        val pending = PendingIntent.getService(
            this,
            requestCode,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Action.Builder(
            Icon.createWithResource(this, R.drawable.ic_launcher_foreground),
            getString(labelRes),
            pending,
        ).build()
    }

    companion object {
        private const val CHANNEL_ID = "voice_call"
        private const val NOTIFICATION_ID = 2002
        private const val REQUEST_TALK = 1
        private const val REQUEST_HANG_UP = 2
        private const val MAX_CALL_MS = 4L * 60 * 60 * 1000
        private const val ACTION_START = "com.agentsanywhere.app.voice.START"
        private const val ACTION_TALK = "com.agentsanywhere.app.voice.TALK"
        private const val ACTION_HANG_UP = "com.agentsanywhere.app.voice.HANG_UP"
        private const val ACTION_SET_RECOGNITION = "com.agentsanywhere.app.voice.SET_RECOGNITION"
        private const val EXTRA_RECOGNITION = "recognition"
        private const val EXTRA_SESSION_ID = "sessionId"
        private const val EXTRA_TITLE = "title"
        private val HEADSET_KEYS = setOf(
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE,
        )

        /** Must be called while the app is in the foreground (Android 14 microphone FGS rule). */
        fun start(context: Context, sessionId: String, title: String) {
            val intent = Intent(context, VoiceCallService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_SESSION_ID, sessionId)
                .putExtra(EXTRA_TITLE, title)
            context.startForegroundService(intent)
        }

        fun talk(context: Context) {
            context.startService(Intent(context, VoiceCallService::class.java).setAction(ACTION_TALK))
        }

        fun setRecognition(context: Context, mode: VoiceRecognitionMode) {
            context.startService(
                Intent(context, VoiceCallService::class.java)
                    .setAction(ACTION_SET_RECOGNITION)
                    .putExtra(EXTRA_RECOGNITION, mode.storageValue),
            )
        }

        fun hangUp(context: Context) {
            context.startService(Intent(context, VoiceCallService::class.java).setAction(ACTION_HANG_UP))
        }
    }
}

internal fun voicePhrases(context: Context): VoicePhrases = VoicePhrases(
    messagePrefix = context.getString(R.string.voice_call_message_prefix) + "\n\n",
    connected = context.getString(R.string.voice_call_say_connected),
    agentAlreadyRunning = context.getString(R.string.voice_call_say_agent_running),
    stillHere = context.getString(R.string.voice_call_say_still_here),
    standby = context.getString(R.string.voice_call_say_standby),
    interrupted = context.getString(R.string.voice_call_say_interrupted),
    nothingRunning = context.getString(R.string.voice_call_say_nothing_running),
    steerUnavailable = context.getString(R.string.voice_call_say_steer_unavailable),
    approvalPrompt = context.getString(R.string.voice_call_say_approval_prompt),
    approvalNeedsScreen = context.getString(R.string.voice_call_say_approval_needs_screen),
    approvalAnswerHint = context.getString(R.string.voice_call_say_approval_hint),
    approvalDone = context.getString(R.string.voice_call_say_approval_done),
    actionFailed = context.getString(R.string.voice_call_say_action_failed),
    turnFailed = context.getString(R.string.voice_call_say_turn_failed),
    noReply = context.getString(R.string.voice_call_say_no_reply),
    workSummaryFiles = context.getString(R.string.voice_call_say_files_changed),
    workSummaryCommands = context.getString(R.string.voice_call_say_commands_run),
    notStarted = context.getString(R.string.voice_call_say_not_started),
    sendFailed = context.getString(R.string.voice_call_say_send_failed),
    nothingToRepeat = context.getString(R.string.voice_call_say_nothing_to_repeat),
    hangUp = context.getString(R.string.voice_call_say_hang_up),
    recognizerUnavailable = context.getString(R.string.voice_call_say_recognizer_unavailable),
    noSpeechHeard = context.getString(R.string.voice_call_no_speech_heard),
    recognitionFailed = context.getString(R.string.voice_call_say_recognition_failed),
    labels = SpeechTextLabels(
        codeOmitted = context.getString(R.string.voice_call_code_omitted),
        tableOmitted = context.getString(R.string.voice_call_table_omitted),
        truncated = context.getString(R.string.voice_call_truncated),
    ),
)
