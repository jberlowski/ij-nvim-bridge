package dev.bridge.brain

import com.intellij.codeInsight.completion.CodeCompletionHandlerBase
import com.intellij.codeInsight.completion.CompletionProcessEx
import com.intellij.codeInsight.completion.CompletionProgressIndicator
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
import java.util.concurrent.atomic.AtomicReference

/**
 * Streaming completion (SPEC.md §6), harvested from IntelliJ's own lookup
 * (ADR-0008).
 *
 * `invokeCompletion` blocks the EDT for ~90% of the time to first items, so
 * requests serialise and the synchronous part cannot be cancelled. A newer
 * request for a buffer therefore *supersedes* an older one instead: at most one
 * in flight and one queued per buffer, newest wins (SPEC.md §6.4).
 */
/** Harness only: slow completion down so an in-flight request can be observed. */
object DebugLevers {
    @Volatile var completionDelayMs = 0L
    @Volatile var navigationDelayMs = 0L
}

class CompletionEngine(private val project: Project) : Disposable {

    class Request(
        val id: JsonElement?,
        val uri: String,
        val position: JsonObject,
        val streamId: String,
        /** How long to wait for IntelliJ's first results before answering empty. A stall guard, not a
         * latency budget: a cold first completion takes seconds, and IntelliJ's own time is never
         * the Bridge's to cap (ADR-0005). */
        val lateWaitMs: Long = 10_000,
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

    /** Where a request was made: what a later `completionItem/resolve` needs to redo the insertion. */
    class Site(val uri: String, val text: String, val offset: Int, val streamId: String)

    /** The elements answered with, so an item the developer accepts can be inserted the way IntelliJ would. */
    val store = ElementStore()

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

    /** The Session went away: nothing queued for it should run, or be answered. */
    fun cancelAllFor(transport: Transport) {
        synchronized(this) { supersedeWhere { it.transport === transport } }
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
        inflight?.takeIf(match)?.superseded = true // finishes; a complete answer is still delivered, flagged
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

        // IntelliJ tells us when it is done (see FinishHandler); until then the
        // in-progress lookup, if there is one, gives early batches.
        val finished = AtomicReference<List<LookupElement>?>(null)

        val tInvoke = System.nanoTime()
        val deadline = tInvoke + r.lateWaitMs * 1_000_000L
        var first: Snap? = null
        var attempts = 0
        var site = Site(r.uri, "", 0, r.streamId)
        while (first == null && !r.superseded && System.nanoTime() < deadline) {
            attempts++
            first = edt {
                LookupManager.hideActiveLookup(project)
                val doc = mirror.document
                val at = MirrorSet.offset(doc, r.position)
                site = Site(r.uri, doc.text, at, r.streamId)
                mirror.editor.caretModel.moveToOffset(at)
                FinishHandler { finished.set(it) }.invokeCompletion(project, mirror.editor)
                snapshot(mirror.editor, sent, finished.get(), site)
            }
            // IntelliJ sometimes declines to start a completion at all - no process,
            // no result - most often for the first request after a file is opened.
            // A completion that is running gets all the time it needs; one that is
            // not running is asked for again.
            var idleMs = 0L
            while (first == null && !r.superseded && System.nanoTime() < deadline) {
                Thread.sleep(POLL_MS)
                val (snap, running) = edt {
                    snapshot(mirror.editor, sent, finished.get(), site) to (inProgress() != null)
                }
                first = snap
                if (first != null) break
                idleMs = if (running) 0 else idleMs + POLL_MS
                if (idleMs >= RETRY_AFTER_MS) break
            }
        }
        // Harness only: make IntelliJ *finish* slowly. After the invocation, not before
        // it: a request replaced while IntelliJ is already working on it is the case
        // that matters, and one replaced before it starts never runs at all.
        if (DebugLevers.completionDelayMs > 0) Thread.sleep(DebugLevers.completionDelayMs)
        if (r.superseded) {
            // A newer keystroke replaced this request, but IntelliJ has already done the
            // work, and the answer is exactly right for the text it was asked about. If it
            // finished in one batch, deliver it flagged `superseded`: the Editor caches it
            // (so backspacing returns to it) and can show it as an interim result for the
            // newer request. An unfinished stream is still dropped.
            val finished = first
            if (finished != null && !finished.calculating) {
                respond(r, finished.fresh, done = true, incomplete = false, tInvoke = tInvoke,
                    snap = finished, superseded = true)
            } else {
                finishSuperseded(r)
            }
            return
        }

        if (first == null) { // IntelliJ produced nothing in time
            val diag = edt {
                buildJsonObject {
                    put("appActive", ApplicationManager.getApplication().isActive)
                    put("editorShowing", mirror.editor.contentComponent.isShowing)
                    put("completionFinished", finished.get() != null)
                    put("phase", com.intellij.codeInsight.completion.impl.CompletionServiceImpl
                        .completionPhase::class.java.simpleName)
                    put("hasProcess", CompletionService.getCompletionService().currentCompletion != null)
                    put("dumb", com.intellij.openapi.project.DumbService.getInstance(project).isDumb)
                    put("attempts", attempts)
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
            last = edt { snapshot(mirror.editor, sent, finished.get(), site) } ?: break
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
                        tInvoke: Long, snap: Snap?, diag: JsonObject? = null, superseded: Boolean = false) {
        val tSend = System.nanoTime()
        r.responded = true
        r.transport.send(Wire.response(r.id, buildJsonObject {
            put("streamId", r.streamId)
            put("items", JsonArray(items))
            put("done", done)
            put("isIncomplete", incomplete)
            if (superseded) put("superseded", true)
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

    /**
     * Must run on the EDT: what IntelliJ has produced so far, rendered once.
     * `finished` is the final list from FinishHandler; without it, whatever the
     * in-progress lookup holds. Null when there is neither yet.
     */
    private fun snapshot(editor: Editor, sent: MutableSet<LookupElement>,
                         finished: List<LookupElement>?, site: Site): Snap? {
        val at = System.nanoTime()
        val items: List<LookupElement>
        val calculating: Boolean
        if (finished != null) {
            items = finished
            calculating = false
        } else {
            val lookup = inProgress() ?: return null
            items = lookup.items
            calculating = true
        }
        val fresh = ArrayList<JsonObject>()
        items.forEachIndexed { index, element ->
            if (sent.add(element)) fresh += render(element, index, site)
        }
        return Snap(fresh, items.size, calculating, at, System.nanoTime() - at)
    }

    /** The lookup of the completion now running, shown or not. */
    private fun inProgress(): LookupImpl? {
        val process = CompletionService.getCompletionService().currentCompletion as? CompletionProcessEx
        return (process?.lookup as? LookupImpl)?.takeUnless { it.isLookupDisposed }
    }

    private fun render(element: LookupElement, index: Int, site: Site): JsonObject {
        val presentation = LookupElementPresentation()
        element.renderElement(presentation)
        val id = "${site.streamId}.$index"
        store.put(id, element, site)
        return buildJsonObject {
            // What `completionItem/resolve` needs: the file, and which element.
            put("data", buildJsonObject {
                put("uri", site.uri)
                put("id", id)
            })
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
        const val RETRY_AFTER_MS = 100L
    }
}


/**
 * Comrade's technique, which is why completion works while Neovim has the focus
 * (beeender/ComradeNeovim, completion/CodeCompletionHandler.kt).
 *
 * The default handler finishes by *showing* a lookup, and IntelliJ shows nothing
 * while it is not the active application - it discards the results instead. The
 * handler does, however, announce that it is done, with the indicator still
 * holding the items. Taking them there never depends on a visible popup.
 *
 * Deliberately no `super` call: it would show a popup and, for a lone candidate,
 * *insert it into the Mirror's document*.
 */
@Suppress("DEPRECATION")
private class FinishHandler(private val onFinished: (List<LookupElement>) -> Unit) :
    CodeCompletionHandlerBase(CompletionType.BASIC, true, false, true) {
    override fun completionFinished(indicator: CompletionProgressIndicator, hasModifiers: Boolean) {
        onFinished(indicator.lookup.items.toList())
    }
}
