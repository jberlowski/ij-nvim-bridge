package dev.bridge.brain

import com.intellij.openapi.Disposable
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.event.DocumentEvent
import com.intellij.openapi.editor.event.DocumentListener
import com.intellij.psi.PsiDocumentManager
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/** An edit made in the IDE that has been sent to the Editor and not yet come back as its own `didChange`. */
class Forward(val foreign: String) {
    @Volatile var applied = false
    /** Another foreign edit was made while this was in flight: replayed once this one has come back. */
    @Volatile var queued = false
}

/** What [ForeignEdits.echoArrives] took out of the way: what to do once the Editor's changes are in. */
class Echo(val forward: Forward, val latest: String)

/**
 * An edit made in the IntelliJ window is forwarded to Neovim, not kept (ADR-0009).
 *
 * A Mirror must equal the Editor's buffer, which is the Source of Truth. Every change the Brain itself makes to a
 * Mirror's Document sets [Mirror.applying]; any other change is *foreign* (typing in the IDE, one of its quick
 * fixes or refactors). Once foreign changes have stopped for [SETTLE_MS], the difference from the text just before
 * the first of them is sent to the owning Session as `workspace/applyEdit`, versioned so that a buffer that has
 * moved on refuses it. Until the Editor's own `didChange` for it comes back the Mirror keeps the IDE's text (it is
 * what the buffer is about to hold); then the Mirror is put back to the earlier text and the Editor's changes are
 * applied as always, which is what stops the edit being applied twice. A refusal puts the Mirror back at once and
 * says so.
 */
class ForeignEdits(private val mirrors: MirrorSet) : Disposable {

    private val log = logger<ForeignEdits>()
    private val scheduler = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "bridge-foreign-edits").apply { isDaemon = true }
    }
    private val dirty = ConcurrentHashMap.newKeySet<Mirror>()
    private var settle: ScheduledFuture<*>? = null

    /** What happened to a foreign edit, for the Brain's log: (uri, outcome). */
    @Volatile var record: ((String, String) -> Unit)? = null

    // ------------------------------------------------------------------ detecting
    fun watch(m: Mirror) {
        val listener = object : DocumentListener {
            override fun beforeDocumentChange(event: DocumentEvent) {
                if (!m.applying && m.base == null) m.base = event.document.text
            }

            override fun documentChanged(event: DocumentEvent) {
                if (!m.applying) changed(m)
            }
        }
        m.listener = listener
        m.document.addDocumentListener(listener)
    }

    fun unwatch(m: Mirror) {
        m.listener?.let { m.document.removeDocumentListener(it) }
        m.listener = null
        dirty -= m
        m.base = null
        m.pending = null
    }

    @Synchronized
    private fun changed(m: Mirror) {
        dirty += m
        settle?.cancel(false)
        settle = scheduler.schedule({ flush() }, SETTLE_MS, TimeUnit.MILLISECONDS)
    }

    // ------------------------------------------------------------------ forwarding
    private fun flush() {
        try {
            val batch = dirty.toList()
            dirty.clear()
            val bySession = LinkedHashMap<Session, MutableList<Pair<Mirror, List<JsonObject>>>>()
            for (m in batch) {
                val base = m.base ?: continue
                m.pending?.let { it.queued = true; continue }
                val current = edt { m.document.text }
                if (current == base) { m.base = null; continue }
                val session = m.owners.singleOrNull() as? Session
                if (session == null) {
                    // Two Editors on one file are unsupported (ADR-0003), and none has nowhere to send it.
                    restore(m, base)
                    record?.invoke(m.uri, "undone: ${m.owners.size} editors")
                    continue
                }
                m.pending = Forward(current)
                bySession.getOrPut(session) { ArrayList() } += m to TextEdits.diff(base, current)
            }
            for ((session, items) in bySession) forward(session, items)
        } catch (t: Throwable) {
            log.warn("bridge: forwarding an IDE edit failed", t)
        }
    }

    private fun forward(session: Session, items: List<Pair<Mirror, List<JsonObject>>>) {
        val changes = items.map { (m, edits) ->
            buildJsonObject {
                put("textDocument", buildJsonObject { put("uri", m.uri); put("version", m.version) })
                put("edits", JsonArray(edits))
            }
        }
        val forwarded = items.map { it.first }
        session.request("workspace/applyEdit", buildJsonObject {
            put("label", "Edit made in IntelliJ")
            put("edit", buildJsonObject { put("documentChanges", JsonArray(changes)) })
        }) { reply -> answered(session, forwarded, reply) }
        forwarded.forEach { record?.invoke(it.uri, "forwarded") }
    }

    private fun answered(session: Session, forwarded: List<Mirror>, reply: JsonObject) {
        try {
            val result = reply["result"] as? JsonObject
            val applied = result?.get("applied")?.jsonPrimitive?.booleanOrNull == true
            for (m in forwarded) {
                val f = m.pending ?: continue          // its echo has already come: the Mirror is the Editor's again
                if (applied) {
                    f.applied = true
                    scheduler.schedule({ giveUp(m, f) }, GIVE_UP_S, TimeUnit.SECONDS)
                } else {
                    m.base?.let { restore(m, it) }
                    record?.invoke(m.uri, "refused")
                }
            }
            if (!applied) {
                val why = result?.get("failureReason")?.jsonPrimitive?.contentOrNull
                    ?: (reply["error"] as? JsonObject)?.get("message")?.jsonPrimitive?.contentOrNull ?: "the Editor refused it"
                val names = forwarded.joinToString(", ") { it.file.name }
                session.notifyClient("window/showMessage", buildJsonObject {
                    put("type", 2) // Warning
                    put("message", "An edit made in IntelliJ to $names was not applied ($why). Neovim's text was kept.")
                })
            }
        } catch (t: Throwable) {
            log.warn("bridge: answering an IDE edit failed", t)
        }
    }

    /** The Editor said it applied the edit and its `didChange` never came: nothing more is to be waited for. */
    private fun giveUp(m: Mirror, f: Forward) {
        if (m.pending !== f) return
        log.warn("bridge: no didChange came back for an IDE edit to ${m.uri}")
        m.pending = null
        m.base = null
        record?.invoke(m.uri, "no echo")
    }

    // ------------------------------------------------------------------ the echo
    /**
     * A `didChange` has arrived for a Mirror with a forward in flight: put the Mirror back to the text before the
     * IDE's edit, silently, so that the Editor's changes (which include that edit) are applied to it as always.
     * Returns what [echoed] needs afterwards, or null when nothing was in flight.
     */
    fun echoArrives(m: Mirror): Echo? {
        val forward = m.pending ?: return null
        val base = m.base ?: return null
        val latest = edt { m.document.text }
        restore(m, base)
        return Echo(forward, latest)
    }

    /** The Editor's changes are in: an IDE edit made meanwhile is forwarded on its own, from where things now stand. */
    fun echoed(m: Mirror, echo: Echo) {
        // Anything past what was sent is an edit made meanwhile (the flag may not be set yet: its settling may not have run).
        if (echo.latest == echo.forward.foreign) return
        edt {
            if (m.document.text != echo.forward.foreign) {
                log.warn("bridge: an IDE edit made while another was forwarded was dropped: the Editor's text moved on")
                record?.invoke(m.uri, "dropped: queued edit")
                return@edt
            }
            // Not flagged: this is a foreign edit again, and is settled and forwarded like the first.
            WriteCommandAction.runWriteCommandAction(m.editor.project) {
                applyEdits(m.document, TextEdits.diff(echo.forward.foreign, echo.latest))
            }
        }
    }

    /** Put the Mirror back to [text], as the Brain's own change (not foreign). */
    private fun restore(m: Mirror, text: String) {
        edt {
            m.applying = true
            try {
                val doc = m.document
                val project = m.editor.project
                WriteCommandAction.runWriteCommandAction(project) { applyEdits(doc, TextEdits.diff(doc.text, text)) }
                if (project != null) PsiDocumentManager.getInstance(project).commitDocument(doc)
            } finally {
                m.applying = false
            }
        }
        m.pending = null
        m.base = null
    }

    override fun dispose() {
        scheduler.shutdownNow()
    }

    companion object {
        /** How long foreign changes must stop for before they are forwarded: a refactor is several, typing one per key. */
        const val SETTLE_MS = 200L
        const val GIVE_UP_S = 15L

        /** Ascending, non-overlapping edits by position, applied last first so that the earlier positions still hold. */
        fun applyEdits(doc: Document, edits: List<JsonObject>) {
            for (edit in edits.asReversed()) {
                val range = edit["range"]!!.jsonObject
                val start = MirrorSet.offset(doc, range["start"]!!.jsonObject)
                val end = MirrorSet.offset(doc, range["end"]!!.jsonObject)
                doc.replaceString(start, end, edit["newText"]!!.jsonPrimitive.content)
            }
        }
    }
}
