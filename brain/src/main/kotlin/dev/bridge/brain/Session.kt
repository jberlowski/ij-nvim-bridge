package dev.bridge.brain

import com.intellij.openapi.diagnostic.logger
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.nio.channels.Channels
import java.nio.channels.SocketChannel
import java.util.UUID

/**
 * One Editor connected to one Brain for one project (CONTEXT.md: Session).
 *
 * Messages are handled in order on the reader thread, which is what makes
 * didChange sequences apply correctly. Completion is the exception: it is
 * queued to the engine, which answers later.
 */
class Session(private val conn: SocketChannel, private val brain: BrainService) {

    private val log = logger<Session>()
    private val transport = Transport(Channels.newOutputStream(conn))

    fun run() {
        brain.register(transport)
        try {
            conn.use {
                val input = Channels.newInputStream(conn)
                while (true) {
                    val message = Wire.read(input) ?: return
                    // t1 of SPEC.md §7 is when the request has arrived, not when we
                    // began waiting for it.
                    val received = System.nanoTime()
                    if (!handle(message, received)) return
                }
            }
        } finally {
            brain.unregister(transport)
        }
    }

    /** False ends the session. */
    private fun handle(message: JsonObject, received: Long): Boolean {
        val method = message["method"]?.jsonPrimitive?.contentOrNull ?: return true
        val id = message["id"]
        val params = message["params"] as? JsonObject ?: buildJsonObject {}
        try {
            when (method) {
                "initialize" -> reply(id, buildJsonObject {
                    put("capabilities", brain.capabilities())
                    put("serverInfo", buildJsonObject { put("name", "ij-nvim-bridge"); put("version", "0.1.0") })
                })
                "initialized" -> {}
                "shutdown" -> reply(id, JsonNull)
                "exit" -> return false

                "textDocument/didOpen" -> {
                    val doc = params.obj("textDocument")
                    brain.mirrors.open(doc.str("uri"), doc.int("version"), doc.str("text"))
                }
                "textDocument/didChange" -> {
                    val doc = params.obj("textDocument")
                    brain.mirrors.change(doc.str("uri"), doc.int("version"),
                        params["contentChanges"]?.jsonArray ?: JsonArray(emptyList()))
                }
                // SPEC.md §5.4: the Editor's "await Brain ack of version N" before it
                // writes. Messages are handled in order, so by the time this reply
                // exists every earlier didChange has been applied. The reply is
                // an empty edit list - the Brain never edits on save (v2).
                "textDocument/willSaveWaitUntil" -> {
                    brain.saveAcks.incrementAndGet()
                    reply(id, JsonArray(emptyList()))
                }
                "textDocument/didSave" -> {
                    val doc = params.obj("textDocument")
                    brain.mirrors.saved(doc.str("uri"), doc.intOr("version", -1))
                }
                "textDocument/didClose" -> {
                    val uri = params.obj("textDocument").str("uri")
                    brain.mirrors.close(uri)
                    brain.diagnostics.clear(uri)
                }
                // The Editor's active buffer changed (FEATURES.md §9).
                "\$/ij/focus" -> brain.mirrors.select(params.obj("textDocument").str("uri"))

                "\$/ij/completion" -> {
                    val uri = params.obj("textDocument").str("uri")
                    brain.completion.submit(CompletionEngine.Request(
                        id = id, uri = uri, position = params.obj("position"),
                        streamId = UUID.randomUUID().toString().take(8),
                        lateWaitMs = params["lateWaitMs"]?.jsonPrimitive?.intOrNull?.toLong()
                            ?: 10_000L,
                        received = received, transport = transport,
                        mirror = { brain.mirrors.get(uri) },
                    ))
                }
                "\$/ij/completionCancel" -> brain.completion.cancel(params.str("streamId"))
                "\$/ij/debug/state" -> reply(id, debugState(params))
                "\$/ij/debug/saveAll" -> {
                    // What an idle IDE or a frame deactivation triggers.
                    edt { com.intellij.openapi.fileEditor.FileDocumentManager.getInstance().saveAllDocuments() }
                    reply(id, JsonNull)
                }
                "\$/ij/debug/setTabLimit" -> {
                    brain.setTabLimit(params.int("limit"))
                    reply(id, JsonNull)
                }

                else -> if (id != null) {
                    transport.send(Wire.error(id, RpcError.METHOD_NOT_FOUND, "unknown method: $method"))
                }
            }
        } catch (t: Throwable) {
            log.warn("bridge: $method failed", t)
            // A visible failure beats a stall: a throwing handler must not
            // leave the client waiting for a reply that never comes.
            if (id != null) {
                transport.send(Wire.error(id, RpcError.INTERNAL, "${t::class.java.simpleName}: ${t.message}"))
            }
        }
        return true
    }

    private fun reply(id: JsonElement?, result: JsonElement) {
        if (id != null) transport.send(Wire.response(id, result))
    }

    /** SPEC.md §10. Exists for the harness; not a user feature. */
    private fun debugState(params: JsonObject): JsonObject {
        val withText = params["text"]?.jsonPrimitive?.contentOrNull == "true"
        return buildJsonObject {
            put("project", brain.projectName())
            put("capabilities", brain.capabilities())
            put("state", brain.state())
            put("evictions", brain.mirrors.evictions.get())
            put("saveAcks", brain.saveAcks.get())
            // Whether IntelliJ is the active application. A developer in Neovim
            // keeps it false; the completion test asserts it, so it cannot pass
            // in a state that would have hidden the bug.
            put("appActive", edt { com.intellij.openapi.application.ApplicationManager.getApplication().isActive })
            put("lookupActive", brain.lookupActive())
            put("mirrors", JsonArray(brain.mirrors.all().map { m ->
                buildJsonObject {
                    put("uri", m.uri)
                    put("version", m.version)
                    put("convergent", m.convergent)
                    put("open", brain.mirrors.isOpen(m))
                    put("showing", edt { m.editor.contentComponent.isShowing })
                    put("length", edt { m.document.textLength })
                    if (withText) put("text", edt { m.document.charsSequence.toString() })
                }
            }))
        }
    }

    private fun JsonObject.obj(key: String): JsonObject =
        this[key]?.jsonObject ?: throw IllegalArgumentException("missing $key")
    private fun JsonObject.str(key: String): String =
        this[key]?.jsonPrimitive?.contentOrNull ?: throw IllegalArgumentException("missing $key")
    private fun JsonObject.int(key: String): Int =
        this[key]?.jsonPrimitive?.intOrNull ?: throw IllegalArgumentException("missing $key")
    private fun JsonObject.intOr(key: String, default: Int): Int =
        this[key]?.jsonPrimitive?.intOrNull ?: default
}
