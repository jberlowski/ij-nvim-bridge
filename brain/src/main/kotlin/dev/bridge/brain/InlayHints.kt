package dev.bridge.brain

import com.intellij.codeInsight.daemon.impl.ParameterHintsPresentationManager
import com.intellij.codeInsight.hints.declarative.impl.inlayRenderer.DeclarativeInlayRenderer
import com.intellij.codeInsight.hints.declarative.impl.views.TextInlayPresentationEntry
import com.intellij.openapi.Disposable
import com.intellij.openapi.editor.Inlay
import com.intellij.openapi.editor.InlayModel
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/**
 * `textDocument/inlayHint` (FEATURES.md §4): parameter names, inferred types, and what else
 * IntelliJ shows inline.
 *
 * Harvested, as diagnostics are, from what IntelliJ itself has put on the Mirror's editor: its
 * own inlay passes run for an editor that is showing, so the hints are exactly the ones the
 * developer's IDE would show *with the developer's own inlay settings* (a Borrowed Setting), for
 * the unsaved text, and no provider is reimplemented. Two kinds of inlay carry the text: the
 * declarative ones (Kotlin's, and newer Java) and the older parameter-name hints.
 *
 * The passes finish a moment after a file is opened or edited, so a request can be answered
 * before they have: the Brain watches each Mirror's inlays and sends `workspace/inlayHint/refresh`
 * when they change, which makes the client ask again.
 */
class InlayHints(private val project: Project, private val brain: BrainService) : Disposable {

    private val watchers = ConcurrentHashMap<String, Disposable>()
    private val lastSeen = ConcurrentHashMap<String, Int>()
    private val requestIds = AtomicInteger()
    private val pending = ConcurrentHashMap<String, java.util.concurrent.ScheduledFuture<*>>()
    private val timer = java.util.concurrent.Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "bridge-inlay-refresh").apply { isDaemon = true }
    }

    // ------------------------------------------------------------------ answer
    /** Must run on the EDT (the inlay model is the editor's). */
    fun hints(mirror: Mirror, range: JsonObject?): JsonElement {
        val doc = mirror.document
        val start = range?.get("start")?.let { MirrorSet.offset(doc, it as JsonObject) } ?: 0
        val end = range?.get("end")?.let { MirrorSet.offset(doc, it as JsonObject) } ?: doc.textLength
        return JsonArray(collect(mirror, start, end).sortedBy { it.offset }.map { hint ->
            buildJsonObject {
                put("position", Locations.position(doc, hint.offset))
                put("label", hint.label)
                hint.kind?.let { put("kind", it) }
                if (hint.kind == PARAMETER) put("paddingRight", true)
            }
        })
    }

    private class Hint(val offset: Int, val label: String, val kind: Int?)

    private fun collect(mirror: Mirror, start: Int, end: Int): List<Hint> {
        val out = ArrayList<Hint>()
        val manager = ParameterHintsPresentationManager.getInstance()
        for (inlay in mirror.editor.inlayModel.getInlineElementsInRange(start, end)) {
            val renderer = inlay.renderer
            when {
                manager.isParameterHint(inlay) -> out += Hint(inlay.offset, manager.getHintText(inlay).orEmpty(), PARAMETER)
                renderer is DeclarativeInlayRenderer -> {
                    val text = renderer.presentationLists.flatMap { entriesOf(it) }
                        .filterIsInstance<TextInlayPresentationEntry>().joinToString("") { it.text }
                    if (text.isNotEmpty()) out += Hint(inlay.offset, text, kindOf(renderer.providerId + " " + renderer.sourceId, text))
                }
            }
        }
        return out
    }

    /**
     * A hint's pieces. The getter is public in the compiled class and hidden from Kotlin, which sees the
     * property as private, so it is called reflectively; if IntelliJ changes it, the hint is skipped and
     * nothing else is affected.
     */
    private fun entriesOf(list: Any): List<Any> = try {
        (list.javaClass.getMethod("getEntries").invoke(list) as Array<*>).filterNotNull()
    } catch (_: Throwable) {
        emptyList()
    }

    /** Parameter names, or types; anything else is sent without a kind. */
    private fun kindOf(source: String, text: String): Int? = when {
        source.contains("param", ignoreCase = true) -> PARAMETER
        source.contains("type", ignoreCase = true) || source.contains("return", ignoreCase = true) || text.startsWith(":") -> TYPE
        else -> null
    }

    // ------------------------------------------------------------------ refresh
    /** A Mirror was opened (or its editor replaced): tell clients when its inlays change. */
    fun watch(mirror: Mirror) {
        unwatch(mirror.uri)
        val disposable = Disposer.newDisposable("bridge-inlays-${mirror.uri}")
        Disposer.register(this, disposable)
        watchers[mirror.uri] = disposable
        // The inlay model is the editor's, so it is touched on the EDT only.
        edt {
            mirror.editor.inlayModel.addListener(object : InlayModel.Listener {
                override fun onAdded(inlay: Inlay<*>) = changed(mirror)
                override fun onUpdated(inlay: Inlay<*>, changeFlags: Int) = changed(mirror)
                override fun onRemoved(inlay: Inlay<*>) = changed(mirror)
            }, disposable)
        }
        // Hints already there when watching starts have not been announced.
        changed(mirror)
    }

    fun unwatch(uri: String) {
        watchers.remove(uri)?.let { Disposer.dispose(it) }
        lastSeen.remove(uri)
        pending.remove(uri)?.cancel(false)
    }

    /** Inlays come and go in bursts while the passes run: wait for them to settle, then ask once if it changed. */
    private fun changed(mirror: Mirror) {
        pending.remove(mirror.uri)?.cancel(false)
        pending[mirror.uri] = timer.schedule({
            try {
                val signature = edt { collect(mirror, 0, mirror.document.textLength).map { "${it.offset}${it.label}" }.hashCode() }
                if (lastSeen.put(mirror.uri, signature) != signature) refresh()
            } catch (_: Throwable) {
                // the Mirror went away while waiting
            }
        }, SETTLE_MS, java.util.concurrent.TimeUnit.MILLISECONDS)
    }

    /** A request to the client, as LSP has it: `workspace/inlayHint/refresh`. Its reply is ignored. */
    private fun refresh() {
        brain.broadcast(buildJsonObject {
            put("jsonrpc", "2.0")
            put("id", "ij-inlay-refresh-${requestIds.incrementAndGet()}")
            put("method", "workspace/inlayHint/refresh")
        })
        brain.record.debug(null, "inlay_refresh")
    }

    override fun dispose() {
        timer.shutdownNow()
    }

    companion object {
        const val TYPE = 1
        const val PARAMETER = 2
        const val SETTLE_MS = 300L
    }
}
