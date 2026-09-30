package dev.bridge.brain

import com.intellij.openapi.Disposable
import com.intellij.openapi.editor.ScrollType
import com.intellij.openapi.editor.event.CaretEvent
import com.intellij.openapi.editor.event.CaretListener
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Caret following, in both directions, for an Editor that asked for it (FEATURES.md §6d). Opt-in: nothing here
 * happens for a Session that has not sent `$/ij/follow { enabled: true }`, so a developer who wants the IDE as a
 * silent analysis engine never has a window jump under them.
 *
 * Editor -> IDE: `$/ij/caret { textDocument, version, position }` moves the Mirror's caret (and scrolls it into
 * view). It carries the buffer's version, because the Editor sends buffer changes debounced and a caret can beat
 * the change it refers to: one ahead of the Mirror waits for the `didChange` that catches up.
 *
 * IDE -> Editor: the caret moving in the Mirror's editor, by the developer clicking or arrowing in the IDE window,
 * is sent (debounced, and only if it changed) as `$/ij/caret { uri, version, position }`. The Brain's *own* moves of
 * that caret (to answer a completion, to compute code actions, to place a following caret) are not the developer's
 * and are never sent: [Mirror.moveCaret] marks them, and so is every change the Brain makes to the text.
 */
class CaretFollower(private val brain: BrainService) : Disposable {

    private val scheduler = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "bridge-carets").apply { isDaemon = true }
    }
    private val pending = ConcurrentHashMap<String, ScheduledFuture<*>>()
    /** What each Mirror's caret was last known to be, either way: the same position is not sent again. */
    private val known = ConcurrentHashMap<String, JsonObject>()

    /** Called when a Mirror gets an editor, and again when it gets a new one. */
    fun watch(m: Mirror) {
        m.editor.caretModel.addCaretListener(object : CaretListener {
            override fun caretPositionChanged(event: CaretEvent) {
                if (m.brainMoves > 0 || m.applying) return
                // An IDE edit not yet forwarded (ForeignEdits): the caret goes with it, not ahead of it.
                if (m.base != null || m.pending != null) return
                schedule(m)
            }
        }, this)
    }

    fun unwatch(uri: String) {
        pending.remove(uri)?.cancel(false)
        known.remove(uri)
    }

    // ------------------------------------------------------------------ IDE -> Editor
    private fun schedule(m: Mirror) {
        pending.compute(m.uri) { _, old ->
            old?.cancel(false)
            scheduler.schedule({ send(m) }, DEBOUNCE_MS, TimeUnit.MILLISECONDS)
        }
    }

    private fun send(m: Mirror) {
        try {
            val session = m.owners.singleOrNull() as? Session ?: return
            if (!session.followCaret) return
            val position = edt { Locations.position(m.document, m.editor.caretModel.offset) }
            if (known.put(m.uri, position) == position) return
            session.notifyClient("\$/ij/caret", buildJsonObject {
                put("uri", m.uri)
                put("version", m.version)
                put("position", position)
            })
        } catch (_: Throwable) {
            // the Mirror went away while waiting
        }
    }

    // ------------------------------------------------------------------ Editor -> IDE
    /** The Editor's cursor moved. */
    fun fromEditor(session: Session, params: JsonObject) {
        if (!session.followCaret) return
        val uri = params["textDocument"]!!.let { (it as JsonObject)["uri"]!!.jsonPrimitive.content }
        val m = brain.mirrors.get(uri) ?: return
        if (m.owners.singleOrNull() !== session) return
        val version = params["version"]?.jsonPrimitive?.content?.toIntOrNull() ?: m.version
        m.wanted = version to (params["position"] as JsonObject)
        applyWanted(m)
    }

    /** The wanted caret, once the Mirror has caught up with the text it refers to; called after every `didChange`. */
    fun applyWanted(m: Mirror) {
        val (version, position) = m.wanted ?: return
        if (version > m.version) return
        m.wanted = null
        edt {
            val at = MirrorSet.offset(m.document, position)
            known[m.uri] = position
            m.moveCaret(at)
            // Only where the developer would see it.
            if (m.editor.contentComponent.isShowing) m.editor.scrollingModel.scrollToCaret(ScrollType.MAKE_VISIBLE)
        }
    }

    override fun dispose() {
        scheduler.shutdownNow()
    }

    companion object {
        const val DEBOUNCE_MS = 100L
    }
}
