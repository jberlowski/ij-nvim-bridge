package dev.bridge.brain

import com.intellij.codeInsight.completion.CodeCompletionHandlerBase
import com.intellij.codeInsight.completion.CompletionService
import com.intellij.codeInsight.completion.CompletionType
import com.intellij.codeInsight.lookup.LookupElement
import com.intellij.codeInsight.lookup.LookupElementPresentation
import com.intellij.codeInsight.lookup.LookupManager
import com.intellij.codeInsight.lookup.impl.LookupImpl
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.Collections
import java.util.IdentityHashMap
import java.util.concurrent.LinkedBlockingDeque

/**
 * Streaming completion (SPEC.md §6), harvested from IntelliJ's own lookup
 * (ADR-0008).
 *
 * `invokeCompletion` blocks the EDT for ~90% of the time to first items, so
 * requests serialise and the synchronous part cannot be cancelled. A newer
 * request for a buffer therefore *supersedes* an older one instead: at most one
 * in flight and one queued per buffer, newest wins (SPEC.md §6.4).
 */
class CompletionEngine(private val project: Project) : Disposable {

    class Request(
        val id: JsonElement?,
        val uri: String,
        val position: JsonObject,
        val streamId: String,
        /** Harness only: how long to keep waiting for a lookup that has not appeared. */
        val lateWaitMs: Long = 0,
        /** t1 of SPEC.md §7: stamped when the request has arrived. */
        val received: Long,
        val transport: Transport,
        val mirror: () -> Mirror?,
    ) {
        @Volatile var superseded = false
        @Volatile var responded = false
    }

    private class Snap(
        val fresh: List<JsonObject>,
        val total: Int,
        val calculating: Boolean,
        val at: Long,
        val renderNanos: Long,
    )

    private val log = logger<CompletionEngine>()
    private val queue = LinkedBlockingDeque<Request>()
    @Volatile private var inflight: Request? = null
    @Volatile private var stopped = false
    private val worker = Thread(::loop, "bridge-completion").apply { isDaemon = true; start() }

    fun submit(request: Request) {
        synchronized(this) {
            supersedeWhere { it.uri == request.uri }
            queue.add(request)
        }
    }

    /** `$/ij/completionCancel`: the same operation as being superseded. */
    fun cancel(streamId: String) {
        synchronized(this) { supersedeWhere { it.streamId == streamId } }
    }

    private fun supersedeWhere(match: (Request) -> Boolean) {
        for (queued in queue.toList()) {
            if (match(queued) && queue.remove(queued)) {
                queued.superseded = true
                replyCancelled(queued) // never reached IntelliJ
            }
        }
        inflight?.takeIf(match)?.superseded = true // finishes; results discarded
    }

    private fun loop() {
        while (!stopped) {
            val request = try { queue.take() } catch (_: InterruptedException) { return }
            if (request.superseded) { replyCancelled(request); continue }
            inflight = request
            try {
                run(request)
            } catch (t: Throwable) {
                log.warn("bridge: completion failed", t)
                if (!request.responded) {
                    request.responded = true
                    request.transport.send(Wire.error(request.id, RpcError.INTERNAL,
                        "${t::class.java.simpleName}: ${t.message}"))
                }
            } finally {
                inflight = null
                // Not edt{}: the response is already sent, and waiting here would
                // hold the next request in the queue behind the popup's teardown
                // - measured as ~13 ms of Overhead. The next request hides any
                // stale lookup itself, before it invokes completion.
                ApplicationManager.getApplication().invokeLater { LookupManager.hideActiveLookup(project) }
            }
        }
    }

    private fun replyCancelled(r: Request) {
        if (r.responded) return
        r.responded = true
        r.transport.send(Wire.error(r.id, RpcError.REQUEST_CANCELLED, "superseded"))
    }

    private fun run(r: Request) {
        val mirror = r.mirror() ?: throw IllegalArgumentException("not mirrored: ${r.uri}")
        val sent: MutableSet<LookupElement> = Collections.newSetFromMap(IdentityHashMap())

        val tInvoke = System.nanoTime()
        var first = edt {
            LookupManager.hideActiveLookup(project)
            val doc = mirror.document
            mirror.editor.caretModel.moveToOffset(MirrorSet.offset(doc, r.position))
            CodeCompletionHandlerBase(CompletionType.BASIC).invokeCompletion(project, mirror.editor)
            snapshot(mirror.editor, sent)
        }
        if (r.superseded) { finishSuperseded(r); return }

        // Optionally keep waiting for a lookup that has not appeared (harness
        // only; the default is not to wait). When none comes, `diag` says why.
        var processSeen = false
        var pollsWaited = 0
        while (first == null && pollsWaited * POLL_MS * 5 < r.lateWaitMs && !r.superseded) {
            Thread.sleep(POLL_MS * 5)
            pollsWaited++
            val (snap, process) = edt {
                snapshot(mirror.editor, sent) to (CompletionService.getCompletionService().currentCompletion != null)
            }
            first = snap
            processSeen = processSeen || process
        }
        if (first == null) { // IntelliJ produced no lookup: nothing to offer here
            val diag = edt {
                buildJsonObject {
                    // While the IDE is not the active application IntelliJ completes
                    // nothing (docs/adr/0008, amendment). This makes that visible.
                    put("appActive", ApplicationManager.getApplication().isActive)
                    put("editorShowing", mirror.editor.contentComponent.isShowing)
                    put("completionProcessSeen", processSeen)
                    put("waitedMs", pollsWaited * POLL_MS * 5)

                }
            }
            respond(r, emptyList(), done = true, incomplete = false, tInvoke = tInvoke, snap = null, diag = diag)
            return
        }
        val tFirstSent = System.nanoTime()
        respond(r, first.fresh, done = !first.calculating, incomplete = false,
            tInvoke = tInvoke, snap = first)
        if (!first.calculating) return

        // IntelliJ is still calculating: coalesce what it produces (SPEC.md §6.1).
        val pending = ArrayList<JsonObject>()
        var lastEmit = tFirstSent
        var capped = false
        var last: Snap = first
        while (last.calculating && !r.superseded) {
            if (System.nanoTime() - r.received > CAP_NANOS) { capped = true; break }
            Thread.sleep(POLL_MS)
            last = edt { snapshot(mirror.editor, sent) } ?: break
            pending += last.fresh
            if (pending.isNotEmpty() && System.nanoTime() - lastEmit >= COALESCE_NANOS && last.calculating) {
                notify(r, pending, done = false, incomplete = false)
                pending.clear()
                lastEmit = System.nanoTime()
            }
        }
        if (r.superseded) { finishSuperseded(r); return }
        notify(r, pending, done = true, incomplete = capped)
    }

    private fun finishSuperseded(r: Request) {
        if (!r.responded) replyCancelled(r)
        else r.transport.send(Wire.notification("\$/ij/completionItems", buildJsonObject {
            put("streamId", r.streamId)
            put("items", JsonArray(emptyList()))
            put("done", true)
            put("superseded", true)
        }))
    }

    private fun respond(r: Request, items: List<JsonObject>, done: Boolean, incomplete: Boolean,
                        tInvoke: Long, snap: Snap?, diag: JsonObject? = null) {
        val tSend = System.nanoTime()
        r.responded = true
        r.transport.send(Wire.response(r.id, buildJsonObject {
            put("streamId", r.streamId)
            put("items", JsonArray(items))
            put("done", done)
            put("isIncomplete", incomplete)
            if (diag != null) put("diag", diag)
            put("timings", buildJsonObject {
                // Queue time behind a superseded request is Overhead, not
                // IJ_TIME (ADR-0008): IJ_TIME starts when invokeCompletion does.
                put("queuedNanos", tInvoke - r.received)
                put("ijFirstItemsNanos", if (snap == null) 0L else snap.at - tInvoke)
                put("renderNanos", snap?.renderNanos ?: 0L)
                put("sendAfterReceiveNanos", tSend - r.received)
            })
        }))
    }

    private fun notify(r: Request, items: List<JsonObject>, done: Boolean, incomplete: Boolean) {
        r.transport.send(Wire.notification("\$/ij/completionItems", buildJsonObject {
            put("streamId", r.streamId)
            put("items", JsonArray(items.toList()))
            put("done", done)
            put("isIncomplete", incomplete)
        }))
    }

    /** Must run on the EDT: reads the lookup and renders each new item once. */
    private fun snapshot(editor: Editor, sent: MutableSet<LookupElement>): Snap? {
        val lookup = LookupManager.getActiveLookup(editor) as? LookupImpl ?: return null
        val at = System.nanoTime()
        val items = lookup.items
        val fresh = ArrayList<JsonObject>()
        items.forEachIndexed { index, element ->
            if (sent.add(element)) fresh += render(element, index)
        }
        return Snap(fresh, items.size, lookup.isCalculating, at, System.nanoTime() - at)
    }

    private fun render(element: LookupElement, index: Int): JsonObject {
        val presentation = LookupElementPresentation()
        element.renderElement(presentation)
        return buildJsonObject {
            put("label", element.lookupString)
            put("insertText", element.lookupString)
            // IntelliJ's own ranking, preserved: the Editor must not re-sort.
            put("sortText", "%06d".format(index))
            presentation.typeText?.let { put("detail", it) }
            presentation.tailText?.let {
                put("labelDetails", buildJsonObject { put("detail", it) })
            }
        }
    }

    override fun dispose() {
        stopped = true
        worker.interrupt()
    }

    companion object {
        const val CAP_NANOS = 300_000_000L
        const val COALESCE_NANOS = 30_000_000L
        const val POLL_MS = 10L
    }
}

