package dev.bridge.brain

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.io.InputStream
import java.io.OutputStream
import java.nio.charset.StandardCharsets

/**
 * LSP framing over JSON-RPC 2.0: a Content-Length header, a blank line, a JSON
 * body. Hand-rolled on kotlinx.serialization rather than LSP4J (ADR-0002); the
 * JSON tree API is enough because only a short list of methods is implemented.
 */
object Wire {
    val json = Json { ignoreUnknownKeys = true }

    /** Blocks for the next message; null at end of stream. */
    fun read(input: InputStream): JsonObject? {
        var length = -1
        while (true) {
            val line = readLine(input) ?: return null
            if (line.isEmpty()) break
            val colon = line.indexOf(':')
            if (colon > 0 && line.substring(0, colon).trim().equals("Content-Length", true)) {
                length = line.substring(colon + 1).trim().toIntOrNull() ?: -1
            }
        }
        if (length < 0) return null
        val body = ByteArray(length)
        var got = 0
        while (got < length) {
            val n = input.read(body, got, length - got)
            if (n < 0) return null
            got += n
        }
        return json.parseToJsonElement(String(body, StandardCharsets.UTF_8)).jsonObject
    }

    private fun readLine(input: InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val c = input.read()
            if (c < 0) return if (sb.isEmpty()) null else sb.toString()
            if (c == '\n'.code) {
                if (sb.endsWith("\r")) sb.setLength(sb.length - 1)
                return sb.toString()
            }
            sb.append(c.toChar())
        }
    }

    fun response(id: JsonElement?, result: JsonElement): JsonObject = buildJsonObject {
        put("jsonrpc", "2.0")
        put("id", id ?: JsonNull)
        put("result", result)
    }

    fun error(id: JsonElement?, code: Int, message: String): JsonObject = buildJsonObject {
        put("jsonrpc", "2.0")
        put("id", id ?: JsonNull)
        put("error", buildJsonObject {
            put("code", code)
            put("message", message)
        })
    }

    fun notification(method: String, params: JsonElement): JsonObject = buildJsonObject {
        put("jsonrpc", "2.0")
        put("method", method)
        put("params", params)
    }
}

/**
 * Serialises writes: completion streams and request replies share one socket.
 *
 * It also keeps the Brain's record of what was asked and answered (BridgeLog): every
 * request is [begin]-ed when it arrives, so its reply can say how long it took.
 */
class Transport(private val out: OutputStream, val session: String = "-", private val log: BridgeLog? = null) {

    private class Pending(val method: String, val at: Long)

    private val pending = java.util.concurrent.ConcurrentHashMap<String, Pending>()

    /** A request has arrived. */
    fun begin(id: JsonElement?, method: String, receivedNanos: Long) {
        if (id != null) pending[id.toString()] = Pending(method, receivedNanos)
    }

    @Synchronized
    fun send(message: JsonObject) {
        val body = Wire.json.encodeToString(JsonObject.serializer(), message)
            .toByteArray(StandardCharsets.UTF_8)
        out.write("Content-Length: ${body.size}\r\n\r\n".toByteArray(StandardCharsets.US_ASCII))
        out.write(body)
        out.flush()
        record(message, body.size)
    }

    private fun record(message: JsonObject, bytes: Int) {
        val log = log ?: return
        try {
            val id = message["id"]
            val method = message["method"]?.jsonPrimitive?.contentOrNull
            if (method != null) {                                   // a notification the Brain sent
                if (!log.enabled(BridgeLog.Level.DEBUG)) return
                val params = message["params"] as? JsonObject
                log.debug(session, "send") {
                    put("method", method)
                    params?.get("uri")?.let { put("uri", it) }
                    (params?.get("items") as? kotlinx.serialization.json.JsonArray)?.let { put("items", it.size) }
                    (params?.get("diagnostics") as? kotlinx.serialization.json.JsonArray)?.let { put("diagnostics", it.size) }
                    params?.get("state")?.let { put("state", it) }
                    params?.get("done")?.let { put("done", it) }
                    log.payload(params)?.let { put("params", it) }
                }
                return
            }
            if (id == null) return
            val started = pending.remove(id.toString())
            val ms = started?.let { (System.nanoTime() - it.at) / 1_000_000.0 }
            val error = message["error"] as? JsonObject
            val result = message["result"]
            val slow = ms != null && ms > SLOW_MS
            val level = if (error != null || slow) BridgeLog.Level.INFO else BridgeLog.Level.DEBUG
            if (!log.enabled(level)) return
            val fields: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit = {
                put("method", started?.method ?: "?")
                put("id", id)
                if (ms != null) put("ms", Math.round(ms * 10) / 10.0)
                put("bytes", bytes)
                if (error != null) {
                    error["code"]?.let { put("code", it) }
                    error["message"]?.let { put("message", it) }
                }
                (result as? kotlinx.serialization.json.JsonArray)?.let { put("n", it.size) }
                ((result as? JsonObject)?.get("items") as? kotlinx.serialization.json.JsonArray)?.let { put("n", it.size) }
                ((result as? JsonObject)?.get("timings"))?.let { put("timings", it) }
                log.payload(result)?.let { put("result", it) }
            }
            when {
                error != null -> log.warn(session, "response", fields)
                slow -> log.info(session, "slow_response", fields)
                else -> log.debug(session, "response", fields)
            }
        } catch (_: Throwable) {
            // recording must never break sending
        }
    }

    companion object {
        /** A reply slower than this is worth a line even when only the Brain's life is being recorded. */
        const val SLOW_MS = 250.0
    }
}

object RpcError {
    const val METHOD_NOT_FOUND = -32601
    const val INVALID_PARAMS = -32602
    const val INTERNAL = -32603
    /** LSP RequestCancelled: what a superseded completion is answered with. */
    const val REQUEST_CANCELLED = -32800
    /** LSP ContentModified: the answer would be stale, or IntelliJ cannot answer yet. */
    const val CONTENT_MODIFIED = -32801
}
