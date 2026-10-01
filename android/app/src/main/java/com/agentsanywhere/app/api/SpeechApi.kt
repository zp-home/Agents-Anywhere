package com.agentsanywhere.app.api


/** `/speech` endpoints: server-side recognition for voice calls (docs/api/speech.md). */
class SpeechApi(
    private val client: ApiClient = ApiClient(),
) {
    fun getStatus(serverUrl: String, authorizationToken: String): RemoteSpeechStatus =
        client.getJson(serverUrl, "/speech/status", authorizationToken).toRemoteSpeechStatus()

    fun transcribe(serverUrl: String, authorizationToken: String, wav: ByteArray): RemoteSpeechTranscription =
        client.postMultipart(
            serverUrl = serverUrl,
            path = "/speech/transcriptions",
            files = listOf(UploadFilePart(name = "utterance.wav", mediaType = "audio/wav", bytes = wav)),
            authorizationToken = authorizationToken,
            fieldName = "audio",
        ).toRemoteSpeechTranscription()
}
