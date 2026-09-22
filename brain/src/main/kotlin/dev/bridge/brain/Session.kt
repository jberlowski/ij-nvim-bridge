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

    /** Short and per connection: printed by the Editor too, so a line in each log can be matched. */
    private val id = UUID.randomUUID().toString().take(6)
    private val transport = Transport(Channels.newOutputStream(conn), id, brain.record)
    private var count = 0L

    fun run() {
        brain.register(transport)
        val opened = System.nanoTime()
        brain.record.info(id, "session_open")
        var why = "closed by the Editor"
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
        } catch (t: Throwable) {
            why = "${t::class.java.simpleName}: ${t.message}"
            brain.record.error(id, "session read loop", t)
            throw t
        } finally {
            brain.record.info(id, "session_close") {
                put("why", why)
                put("messages", count)
                put("seconds", (System.nanoTime() - opened) / 1_000_000_000)
            }
            brain.unregister(transport)
            // The editor is gone, cleanly or not: its claims go with it.
            brain.mirrors.dropOwner(this).forEach { brain.diagnostics.clear(it); brain.runnables.clear(it) }
            brain.completion.cancelAllFor(transport)
        }
    }

    /** False ends the session. */
    private fun handle(message: JsonObject, arrived: Long): Boolean {
        val method = message["method"]?.jsonPrimitive?.contentOrNull ?: return true
        val id = message["id"]
        val params = message["params"] as? JsonObject ?: buildJsonObject {}
        count++
        transport.begin(id, method, arrived)
        recordArrival(method, id, params)
        try {
            when (method) {
                "initialize" -> reply(id, buildJsonObject {
                    put("capabilities", brain.capabilities())
                    put("serverInfo", buildJsonObject {
                        put("name", "ij-nvim-bridge"); put("version", "0.1.0")
                        put("session", this@Session.id)
                        brain.record.path?.let { put("log", it.toString()) }
                    })
                })
                // Tell this Editor where the Brain stands, so it never has to ask.
                "initialized" -> transport.send(Wire.notification("\$/ij/status", brain.status().toJson()))
                "shutdown" -> reply(id, JsonNull)
                "exit" -> return false

                "textDocument/didOpen" -> {
                    val doc = params.obj("textDocument")
                    brain.mirrors.open(doc.str("uri"), doc.int("version"), doc.str("text"), this)
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
                    brain.mirrors.close(uri, this)
                    if (brain.mirrors.get(uri) == null) { brain.diagnostics.clear(uri); brain.runnables.clear(uri) }
                }
                in NavigationEngine.METHODS -> brain.navigation.submit(method, id, params, transport)
                "\$/cancelRequest" -> brain.navigation.cancel(params["id"])
                // The Editor's active buffer changed (FEATURES.md §9).
                "\$/ij/focus" -> brain.mirrors.select(params.obj("textDocument").str("uri"))

                "\$/ij/completion" -> {
                    val uri = params.obj("textDocument").str("uri")
                    brain.completionRequests.incrementAndGet()
                    val status = brain.status()
                    if (status.state != "Ready") {
                        // SPEC.md §8: Degraded, not an error and not silence. IntelliJ
                        // would throw IndexNotReadyException and raise an IDE
                        // notification nobody is looking at.
                        transport.send(Wire.response(id, buildJsonObject {
                            put("streamId", "-")
                            put("items", JsonArray(emptyList()))
                            put("done", true)
                            put("isIncomplete", true)
                            put("degraded", true)
                            put("state", status.state)
                            status.reason?.let { put("reason", it) }
                        }))
                        return true
                    }
                    brain.completion.submit(CompletionEngine.Request(
                        id = id, uri = uri, position = params.obj("position"),
                        streamId = UUID.randomUUID().toString().take(8),
                        lateWaitMs = params["lateWaitMs"]?.jsonPrimitive?.intOrNull?.toLong()
                            ?: 10_000L,
                        received = arrived, transport = transport,
                        mirror = { brain.mirrors.get(uri) },
                    ))
                }
                "workspace/didRenameFiles", "workspace/didCreateFiles", "workspace/didDeleteFiles" ->
                    brain.refreshFiles(async = true)
                "\$/ij/completionCancel" -> brain.completion.cancel(params.str("streamId"))
                "\$/ij/debug/state" -> reply(id, debugState(params))
                "\$/ij/debug/saveAll" -> {
                    // What an idle IDE or a frame deactivation triggers.
                    edt { com.intellij.openapi.fileEditor.FileDocumentManager.getInstance().saveAllDocuments() }
                    reply(id, JsonNull)
                }
                "\$/ij/debug/completionDelay" -> {
                    DebugLevers.completionDelayMs = params.int("ms").toLong()
                    reply(id, JsonNull)
                }
                "\$/ij/debug/navigationDelay" -> {
                    DebugLevers.navigationDelayMs = params.int("ms").toLong()
                    reply(id, JsonNull)
                }
                // The log's level, and where it is: for the Editor's `:IjBridge log`, and the harness.
                "\$/ij/log" -> {
                    params["level"]?.jsonPrimitive?.contentOrNull?.let { name ->
                        BridgeLog.parse(name)?.let { brain.record.level = it }
                    }
                    params["maxBytes"]?.jsonPrimitive?.intOrNull?.let { brain.record.maxBytes = it.toLong() }
                    reply(id, buildJsonObject {
                        put("level", brain.record.level.name.lowercase())
                        brain.record.path?.let { put("path", it.toString()) }
                        put("session", this@Session.id)
                    })
                }
                "\$/ij/debug/openInNewWindow" -> {
                    // Harness only. `idea <another project>` in a running IDE asks "this window or a new one?"
                    // unless told, and a modal question nobody answers blocks the IDE (and this Brain). A developer
                    // with two projects has answered it once; this answers it the same way, for the run.
                    edt {
                        com.intellij.ide.GeneralSettings.getInstance().confirmOpenNewProject =
                            com.intellij.ide.GeneralSettings.OPEN_PROJECT_NEW_WINDOW
                    }
                    reply(id, JsonNull)
                }
                // Gradle tasks (FEATURES.md §6d): the tasks IntelliJ has imported, run through its own Gradle integration.
                "\$/ij/tasks" -> reply(id, com.intellij.openapi.application.ReadAction.compute<JsonElement, RuntimeException> {
                    brain.gradle.list()
                })
                "\$/ij/task/run" -> reply(id, brain.gradle.run(params, transport))
                "\$/ij/task/cancel" -> reply(id, brain.gradle.cancel(params["runId"]?.jsonPrimitive?.contentOrNull))
                // Run the test (FEATURES.md §6c): the marker's own Run action, performed for a position.
                "\$/ij/run" -> {
                    val uri = params.obj("textDocument").str("uri")
                    val mirror = brain.mirrors.get(uri)
                    if (mirror == null) transport.send(Wire.error(id, RpcError.INVALID_PARAMS, "not mirrored: $uri"))
                    else reply(id, brain.testRunner.run(mirror, params, transport))
                }
                "\$/ij/run/cancel" -> reply(id, brain.testRunner.cancel(params["runId"]?.jsonPrimitive?.contentOrNull))
                "\$/ij/debug/refresh" -> {
                    brain.refreshFiles()
                    reply(id, JsonNull)
                }
                "\$/ij/debug/indexing" -> {
                    brain.simulateIndexing(params.int("ms").toLong())
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
        } catch (e: Rename.Refused) {
            // Asked for what cannot be done, and why: the client's mistake or state, not a failure of the Brain.
            if (id != null) transport.send(Wire.error(id, RpcError.INVALID_PARAMS, e.message ?: "refused"))
        } catch (t: Throwable) {
            log.warn("bridge: $method failed", t)
            brain.record.error(this.id, method, t)
            // A visible failure beats a stall: a throwing handler must not
            // leave the client waiting for a reply that never comes.
            if (id != null) {
                transport.send(Wire.error(id, RpcError.INTERNAL, "${t::class.java.simpleName}: ${t.message}"))
            }
        }
        return true
    }

    /** What arrived, in a line: which message, about what, never its text. */
    private fun recordArrival(method: String, id: JsonElement?, params: JsonObject) {
        if (!brain.record.enabled(BridgeLog.Level.DEBUG)) return
        brain.record.debug(this.id, "recv") {
            put("method", method)
            if (id != null) put("id", id)
            val doc = params["textDocument"] as? JsonObject
            (doc?.get("uri") ?: (params["data"] as? JsonObject)?.get("uri"))?.let { put("uri", it) }
            doc?.get("version")?.let { put("version", it) }
            (params["contentChanges"] as? JsonArray)?.let { put("changes", it.size) }
            (doc?.get("text") as? kotlinx.serialization.json.JsonPrimitive)?.let { put("length", it.content.length) }
            (params["position"] as? JsonObject)?.let { p ->
                put("at", "${p["line"]}:${p["character"]}")
            }
            params["query"]?.let { put("query", it) }
            params["newName"]?.let { put("newName", it) }
            (params["files"] as? JsonArray)?.let { put("files", it.size) }
            brain.record.payload(params)?.let { put("params", it) }
        }
    }

    private fun reply(id: JsonElement?, result: JsonElement) {
        if (id != null) transport.send(Wire.response(id, result))
    }

    /** SPEC.md §10. Exists for the harness; not a user feature. */
    private fun debugState(params: JsonObject): JsonObject {
        val withText = params["text"]?.jsonPrimitive?.contentOrNull == "true"
        return buildJsonObject {
            put("project", brain.projectName())
            brain.projectRoot()?.let { put("root", it) }
            put("capabilities", brain.capabilities())
            brain.status().let {
                put("state", it.state)
                it.reason?.let { r -> put("reason", r) }
            }
            put("evictions", brain.mirrors.evictions.get())
            put("saveAcks", brain.saveAcks.get())
            put("completionRequests", brain.completionRequests.get())
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
