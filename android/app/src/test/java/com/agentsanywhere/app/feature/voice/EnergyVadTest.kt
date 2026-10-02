package com.agentsanywhere.app.feature.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin

class EnergyVadTest {
    private val frameSamples = 320

    private fun frame(amplitude: Double, seed: Int = 0): ShortArray =
        ShortArray(frameSamples) { index -> (amplitude * sin(2 * PI * 440 * (index + seed) / 16_000)).toInt().toShort() }

    private fun feed(vad: EnergyVad, frames: List<ShortArray>): Pair<EnergyVad.Result, Int> {
        frames.forEachIndexed { index, next ->
            val result = vad.accept(next)
            if (result != EnergyVad.Result.Continue) return result to index + 1
        }
        return EnergyVad.Result.Continue to frames.size
    }

    @Test
    fun cutsUtteranceAfterTrailingSilenceAndKeepsPreRoll() {
        val quiet = List(25) { frame(60.0) } // 500 ms background
        val speech = List(50) { frame(6_000.0, it) } // 1 s speech
        val silence = List(80) { frame(60.0) } // 1.6 s silence
        val (result, consumed) = feed(EnergyVad(), quiet + speech + silence)

        assertTrue(result is EnergyVad.Result.Utterance)
        // Speech ends after frame 75; 1.2 s (60 frames) of silence closes it.
        assertEquals(25 + 50 + 60, consumed)
        val samples = (result as EnergyVad.Result.Utterance).samples
        val frames = samples.size / frameSamples
        // Speech is confirmed on its 6th frame: the 15-frame pre-roll (9 quiet + 6 speech)
        // is kept, then the remaining 44 speech frames and the 60 closing silent frames.
        assertEquals(15 + (50 - 6) + 60, frames)
    }

    @Test
    fun reportsNoSpeechAfterTimeout() {
        val (result, consumed) = feed(EnergyVad(noSpeechTimeoutMs = 2_000), List(200) { frame(80.0) })

        assertEquals(EnergyVad.Result.NoSpeech, result)
        assertEquals(100, consumed)
    }

    @Test
    fun shortClicksDoNotStartSpeech() {
        val frames = List(20) { frame(50.0) } + List(3) { frame(8_000.0) } + List(80) { frame(50.0) }
        val (result, _) = feed(EnergyVad(noSpeechTimeoutMs = 2_000), frames)

        assertEquals(EnergyVad.Result.NoSpeech, result)
    }

    @Test
    fun capsVeryLongUtterances() {
        val frames = List(20) { frame(50.0) } + List(400) { frame(6_000.0, it) }
        val (result, _) = feed(EnergyVad(maxUtteranceMs = 3_000), frames)

        assertTrue(result is EnergyVad.Result.Utterance)
        assertEquals(150, (result as EnergyVad.Result.Utterance).samples.size / frameSamples)
    }

    @Test
    fun endsWhenSteadyNoiseFollowsSpeech() {
        // A fan at ~20% of the speech level after the sentence: the old floor-only rule
        // (0.7 x start threshold) treated it as speech and recorded until the 55 s cap.
        val quiet = List(25) { frame(60.0) }
        val speech = List(50) { frame(6_000.0, it) }
        val fan = List(150) { frame(1_200.0, it) }
        val (result, consumed) = feed(EnergyVad(), quiet + speech + fan)

        assertTrue(result is EnergyVad.Result.Utterance)
        assertEquals(EnergyVad.EndReason.Silence, (result as EnergyVad.Result.Utterance).endReason)
        assertEquals(25 + 50 + 60, consumed)
    }

    @Test
    fun detectsQuietSpeakerAboveMinimumThreshold() {
        // RMS ~283: below the old fixed 350 threshold, so it was never detected.
        val quiet = List(25) { frame(30.0) }
        val softSpeech = List(50) { frame(400.0, it) }
        val silence = List(80) { frame(30.0) }
        val vad = EnergyVad()
        val (result, _) = feed(vad, quiet + softSpeech + silence)

        assertTrue(result is EnergyVad.Result.Utterance)
        // Speech starts with frame 26, i.e. 25 frames x 20 ms into the recording.
        assertEquals(25 * 20, vad.stats.speechStartMs)
    }

    @Test
    fun detectsSoftSpeakerBelowFormerMinimumThreshold() {
        // RMS ~141: below the former 200 minimum, so speaking softly was reported as no speech.
        val quiet = List(25) { frame(30.0) }
        val softSpeech = List(50) { frame(200.0, it) }
        val silence = List(80) { frame(30.0) }
        val vad = EnergyVad()
        val (result, _) = feed(vad, quiet + softSpeech + silence)

        assertTrue(result is EnergyVad.Result.Utterance)
        assertEquals(EnergyVad.EndReason.Silence, (result as EnergyVad.Result.Utterance).endReason)
        assertEquals(25 * 20, vad.stats.speechStartMs)
    }
}
