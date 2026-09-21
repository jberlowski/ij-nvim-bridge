package dev.bridge.brain

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter

/**
 * The Brain's own record of what happened, for finding out why something went wrong on
 * somebody else's machine. One JSON object per line, in the state directory
 * (`$XDG_STATE_HOME/ij-nvim-bridge/brain-<project>.log`), rotated so it can never grow
 * without bound.
 *
 * What is recorded is *metadata*: which message, from which Session, how long it took,
 * what it answered, what failed and why. Never the developer's code. `trace` adds the
 * payloads (truncated) and is opt-in, since it does contain code.
 *
 *  - `info`  the Brain's life: start, Sessions, state changes, failures, anything slow.
 *  - `debug` (the default) every message in and out, summarised.
 *  - `trace` as `debug`, with the parameters and results.
 *  - `off`
 *
 * Set with the environment variable `IJ_NVIM_BRIDGE_LOG`, or at run time (`$/ij/log`).
 * The Editor keeps a matching log; lines are joined by Session id, printed by both.
 */
class BridgeLog(val path: Path?) {

    enum class Level { OFF, INFO, DEBUG, TRACE }

    @Volatile var level: Level = parse(System.getenv("IJ_NVIM_BRIDGE_LOG")) ?: Level.DEBUG
    @Volatile var maxBytes: Long = 5L * 1024 * 1024
    private val keep = 3

    private var out: FileOutputStream? = null
    private var written = 0L

    fun enabled(at: Level) = path != null && level != Level.OFF && at.ordinal <= level.ordinal

    fun info(session: String?, event: String, fields: JsonObjectBuilder.() -> Unit = {}) = write(Level.INFO, "info", session, event, fields)
    fun debug(session: String?, event: String, fields: JsonObjectBuilder.() -> Unit = {}) = write(Level.DEBUG, "debug", session, event, fields)
    fun warn(session: String?, event: String, fields: JsonObjectBuilder.() -> Unit = {}) = write(Level.INFO, "warn", session, event, fields)

    /** A failure, with enough of the stack to find it. */
    fun error(session: String?, where: String, t: Throwable) = write(Level.INFO, "error", session, "error") {
        put("where", where)
        put("type", t::class.java.name)
        put("message", t.message ?: "")
        put("stack", t.stackTrace.take(14).joinToString("\n") { "  at $it" })
        t.cause?.let { put("cause", "${it::class.java.name}: ${it.message}") }
    }

    /** Payloads, for `trace` only. Truncated: a payload is a file's text. */
    fun payload(element: JsonElement?): String? {
        if (!enabled(Level.TRACE) || element == null) return null
        val text = element.toString()
        return if (text.length > 2000) text.take(2000) + "...(${text.length} chars)" else text
    }

    @Synchronized
    private fun write(at: Level, label: String, session: String?, event: String, fields: JsonObjectBuilder.() -> Unit) {
        if (!enabled(at)) return
        try {
            val line = buildJsonObject {
                put("t", OffsetDateTime.now().format(TIME))
                put("lvl", label)
                if (session != null) put("sess", session)
                put("ev", event)
                fields()
            }.toString() + "\n"
            val bytes = line.toByteArray(StandardCharsets.UTF_8)
            val stream = stream()
            stream.write(bytes)
            stream.flush()
            written += bytes.size
            if (written > maxBytes) rotate()
        } catch (_: Throwable) {
            // A log must never be the reason something fails.
        }
    }

    private fun stream(): FileOutputStream {
        out?.let { return it }
        val file = path!!
        Files.createDirectories(file.parent)
        val fresh = !Files.exists(file)
        val stream = FileOutputStream(file.toFile(), true)
        if (fresh) runCatching { Files.setPosixFilePermissions(file, PosixFilePermissions.fromString("rw-------")) }
        written = Files.size(file)
        out = stream
        return stream
    }

    private fun rotate() {
        out?.close()
        out = null
        val file = path ?: return
        for (n in keep - 1 downTo 1) {
            val from = file.resolveSibling("${file.fileName}.$n")
            if (Files.exists(from)) Files.move(from, file.resolveSibling("${file.fileName}.${n + 1}"),
                java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        }
        Files.move(file, file.resolveSibling("${file.fileName}.1"), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        written = 0
    }

    @Synchronized
    fun close() {
        out?.close()
        out = null
    }

    companion object {
        private val TIME = DateTimeFormatter.ISO_OFFSET_DATE_TIME

        fun parse(text: String?): Level? = when (text?.lowercase()) {
            "off" -> Level.OFF
            "info" -> Level.INFO
            "debug" -> Level.DEBUG
            "trace" -> Level.TRACE
            else -> null
        }

        /** `$XDG_STATE_HOME/ij-nvim-bridge`, or `~/.local/state/ij-nvim-bridge`. */
        fun dir(): Path {
            val base = System.getenv("XDG_STATE_HOME")?.takeIf { it.isNotBlank() }?.let { Path.of(it) }
                ?: Path.of(System.getProperty("user.home"), ".local", "state")
            return base.resolve("ij-nvim-bridge")
        }

        fun forProject(hash: String): BridgeLog = BridgeLog(dir().resolve("brain-$hash.log"))
    }
}
