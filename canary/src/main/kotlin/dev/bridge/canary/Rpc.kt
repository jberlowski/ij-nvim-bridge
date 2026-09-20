package dev.bridge.canary

import java.io.InputStream
import java.io.OutputStream
import java.nio.charset.StandardCharsets

/**
 * Minimal LSP-style framing: a Content-Length header, a blank line, a JSON body.
 *
 * Hand-rolled rather than pulling in LSP4J, per ADR-0002 — and this file is most
 * of the evidence for that decision being cheap. The canary hand-rolls the JSON
 * too, because it only ever emits a flat object; the Bridge will use
 * kotlinx.serialization instead.
 */
object Rpc {

    fun readMessage(input: InputStream): String? {
        var contentLength = -1
        while (true) {
            val line = readLine(input) ?: return null
            if (line.isEmpty()) break
            val idx = line.indexOf(':')
            if (idx > 0 && line.substring(0, idx).trim().equals("Content-Length", true)) {
                contentLength = line.substring(idx + 1).trim().toIntOrNull() ?: -1
            }
        }
        if (contentLength < 0) return null
        val body = ByteArray(contentLength)
        var read = 0
        while (read < contentLength) {
            val n = input.read(body, read, contentLength - read)
            if (n < 0) return null
            read += n
        }
        return String(body, StandardCharsets.UTF_8)
    }

    fun writeMessage(output: OutputStream, json: String) {
        val body = json.toByteArray(StandardCharsets.UTF_8)
        output.write("Content-Length: ${body.size}\r\n\r\n".toByteArray(StandardCharsets.US_ASCII))
        output.write(body)
        output.flush()
    }

    private fun readLine(input: InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val c = input.read()
            if (c < 0) return if (sb.isEmpty()) null else sb.toString()
            if (c == '\n'.code) return sb.removeSuffix("\r").toString()
            sb.append(c.toChar())
        }
    }

    private fun StringBuilder.removeSuffix(s: String): StringBuilder {
        if (length >= s.length && substring(length - s.length) == s) setLength(length - s.length)
        return this
    }

    // ------------------------------------------------------------------ json
    fun quote(value: String): String {
        val sb = StringBuilder("\"")
        for (ch in value) {
            when (ch) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> if (ch < ' ') sb.append("\\u%04x".format(ch.code)) else sb.append(ch)
            }
        }
        return sb.append('"').toString()
    }

    fun obj(vararg fields: Pair<String, String>): String =
        fields.joinToString(",", "{", "}") { (k, v) -> "${quote(k)}:$v" }

    fun arr(items: List<String>): String = items.joinToString(",", "[", "]")

    /** Extracts a top-level string value. Enough for {"method":"..."}. */
    fun stringField(json: String, key: String): String? {
        val marker = "\"$key\""
        val at = json.indexOf(marker)
        if (at < 0) return null
        var i = json.indexOf(':', at + marker.length)
        if (i < 0) return null
        i++
        while (i < json.length && json[i].isWhitespace()) i++
        if (i >= json.length || json[i] != '"') return null
        i++
        val sb = StringBuilder()
        while (i < json.length && json[i] != '"') {
            if (json[i] == '\\' && i + 1 < json.length) i++
            sb.append(json[i]); i++
        }
        return sb.toString()
    }
}
