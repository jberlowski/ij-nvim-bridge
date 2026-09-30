package dev.bridge.brain

import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Gradle's output reaches the Brain in tiny pieces (a run of one small test class was 8,329 notifications, about
 * 2 MB, for 20 seconds: 400 messages a second, each mostly envelope). This gathers it and sends it in a few:
 * whatever has come in is sent after [FLUSH_MS], or at once when [MAX_CHARS] have piled up. Order is kept exactly,
 * including between stdout and stderr, and [flush] must be called before anything that must come *after* the
 * output (the end of the run).
 */
class OutputBatcher(private val emit: (text: String, stdout: Boolean) -> Unit) {

    private class Segment(val stdout: Boolean, val text: StringBuilder)

    private val segments = ArrayList<Segment>()
    private var size = 0
    private var scheduled: ScheduledFuture<*>? = null

    @Synchronized
    fun add(text: String, stdout: Boolean) {
        val last = segments.lastOrNull()
        if (last != null && last.stdout == stdout) last.text.append(text) else segments += Segment(stdout, StringBuilder(text))
        size += text.length
        if (size >= MAX_CHARS) flush()
        else if (scheduled == null) scheduled = SCHEDULER.schedule({ flush() }, FLUSH_MS, TimeUnit.MILLISECONDS)
    }

    /** Send what is waiting, now. */
    @Synchronized
    fun flush() {
        scheduled?.cancel(false)
        scheduled = null
        val out = segments.toList()
        segments.clear()
        size = 0
        for (segment in out) {
            try {
                emit(segment.text.toString(), segment.stdout)
            } catch (_: Throwable) {
                // the Editor went away while it ran
            }
        }
    }

    companion object {
        const val FLUSH_MS = 50L
        const val MAX_CHARS = 16_384
        private val SCHEDULER = Executors.newSingleThreadScheduledExecutor { r ->
            Thread(r, "bridge-output").apply { isDaemon = true }
        }
    }
}
