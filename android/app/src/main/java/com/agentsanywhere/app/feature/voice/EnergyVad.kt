package com.agentsanywhere.app.feature.voice

import kotlin.math.max
import kotlin.math.sqrt

/**
 * Energy-based end-of-utterance detection for 16-bit PCM frames.
 *
 * - Noise floor: the quietest frame of the last [floorWindowMs] while not speaking,
 *   frozen once speech starts so the speech itself never raises it.
 * - Speech starts when the level stays above `max(minThreshold, floor × startFactor)`
 *   for [minSpeechMs].
 * - Speech ends after [endSilenceMs] below `max(floor × endFloorFactor, peak × endPeakRatio)`,
 *   where `peak` is the loudest frame of this utterance. The peak-relative part lets a
 *   fan or air conditioner that starts mid-sentence still count as silence.
 * - [preRollMs] of audio before the detected start is kept so the first syllable is not clipped.
 */
class EnergyVad(
    private val frameMs: Int = 20,
    private val minSpeechMs: Int = 120,
    private val endSilenceMs: Int = 1_200,
    private val noSpeechTimeoutMs: Int = 8_000,
    private val maxUtteranceMs: Int = 55_000,
    private val preRollMs: Int = 300,
    private val floorWindowMs: Int = 1_500,
    private val minThreshold: Double = 200.0,
    private val startFactor: Double = 2.5,
    private val endFloorFactor: Double = 1.75,
    private val endPeakRatio: Double = 0.25,
) {
    enum class EndReason { Silence, MaxLength }

    sealed interface Result {
        data object Continue : Result
        data object NoSpeech : Result
        class Utterance(val samples: ShortArray, val endReason: EndReason) : Result
    }

    data class Stats(
        val elapsedMs: Int,
        val noiseFloor: Double,
        val startThreshold: Double,
        val endThreshold: Double,
        val peak: Double,
        val maxLevel: Double,
        val speechStartMs: Int?,
    ) {
        val speechMs: Int
            get() = speechStartMs?.let { elapsedMs - it } ?: 0
    }

    private val preRoll = ArrayDeque<ShortArray>()
    private val floorWindow = ArrayDeque<Double>()
    private val utterance = mutableListOf<ShortArray>()
    private var elapsedMs = 0
    private var noiseFloor = 0.0
    private var peak = 0.0
    private var maxLevel = 0.0
    private var loudMs = 0
    private var silenceMs = 0
    private var speechStartMs: Int? = null

    private val speaking: Boolean
        get() = speechStartMs != null

    private val startThreshold: Double
        get() = max(minThreshold, noiseFloor * startFactor)

    private val endThreshold: Double
        get() = max(noiseFloor * endFloorFactor, peak * endPeakRatio)

    val stats: Stats
        get() = Stats(elapsedMs, noiseFloor, startThreshold, endThreshold, peak, maxLevel, speechStartMs)

    fun accept(frame: ShortArray): Result {
        elapsedMs += frameMs
        val level = rms(frame)
        maxLevel = max(maxLevel, level)
        if (!speaking) {
            trackFloor(level)
            remember(frame)
            if (level > startThreshold) {
                loudMs += frameMs
                if (loudMs >= minSpeechMs) {
                    speechStartMs = elapsedMs - loudMs
                    peak = preRoll.takeLast(loudMs / frameMs).maxOf(::rms)
                    utterance += preRoll
                    preRoll.clear()
                }
            } else {
                loudMs = 0
            }
            return if (!speaking && elapsedMs >= noSpeechTimeoutMs) Result.NoSpeech else Result.Continue
        }
        utterance += frame
        peak = max(peak, level)
        silenceMs = if (level > endThreshold) 0 else silenceMs + frameMs
        return when {
            silenceMs >= endSilenceMs -> Result.Utterance(flatten(), EndReason.Silence)
            utterance.size * frameMs >= maxUtteranceMs -> Result.Utterance(flatten(), EndReason.MaxLength)
            else -> Result.Continue
        }
    }

    private fun trackFloor(level: Double) {
        floorWindow.addLast(level)
        while (floorWindow.size * frameMs > floorWindowMs) floorWindow.removeFirst()
        noiseFloor = floorWindow.min()
    }

    private fun remember(frame: ShortArray) {
        preRoll.addLast(frame)
        while (preRoll.size * frameMs > preRollMs) preRoll.removeFirst()
    }

    private fun flatten(): ShortArray {
        val total = utterance.sumOf { it.size }
        val out = ShortArray(total)
        var offset = 0
        utterance.forEach { chunk ->
            chunk.copyInto(out, offset)
            offset += chunk.size
        }
        return out
    }

    companion object {
        fun rms(frame: ShortArray): Double {
            if (frame.isEmpty()) return 0.0
            var sum = 0.0
            frame.forEach { sample -> sum += sample.toDouble() * sample }
            return sqrt(sum / frame.size)
        }
    }
}
