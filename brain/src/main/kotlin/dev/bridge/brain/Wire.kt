package dev.bridge.brain

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
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

/** Serialises writes: completion streams and request replies share one socket. */
class Transport(private val out: OutputStream) {
    @Synchronized
    fun send(message: JsonObject) {
        val body = Wire.json.encodeToString(JsonObject.serializer(), message)
            .toByteArray(StandardCharsets.UTF_8)
        out.write("Content-Length: ${body.size}\r\n\r\n".toByteArray(StandardCharsets.US_ASCII))
        out.write(body)
        out.flush()
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
