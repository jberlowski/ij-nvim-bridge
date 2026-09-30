package dev.bridge.brain

import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.command.undo.UndoUtil
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerListener
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.fileEditor.ex.FileEditorManagerEx
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.psi.PsiDocumentManager
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.net.URI
import java.nio.file.Path
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/** A genuine IntelliJ editor bound to one Editor buffer (CONTEXT.md: Mirror). */
class Mirror(
    val uri: String,
    val file: VirtualFile,
    val document: Document,
    @Volatile var editor: Editor,
    @Volatile var version: Int,
) {
    /** Holds exactly the buffer's bytes at [version]: every change was applied. */
    @Volatile var convergent: Boolean = true

    // An edit made in the IDE rather than by the Editor (ForeignEdits, ADR-0009).
    /** True while the Brain itself is changing the Document: those changes are not foreign. */
    @Volatile var applying: Boolean = false
    /** The text just before the first foreign change, while there is one not yet settled or answered. */
    @Volatile var base: String? = null
    /** An IDE edit sent to the Editor and not yet echoed back. */
    @Volatile var pending: Forward? = null
    var listener: com.intellij.openapi.editor.event.DocumentListener? = null

    // Caret following (Carets.kt).
    /** Above zero while the Brain moves the caret itself: that is not the developer's movement. */
    @Volatile var brainMoves: Int = 0
    /** A caret from the Editor for a version the Mirror has not reached yet: (version, position). */
    @Volatile var wanted: Pair<Int, JsonObject>? = null

    /** Move the caret as the Brain (to answer a request); on the EDT. Never reported to the Editor as a movement. */
    fun moveCaret(offset: Int) = byBrain { editor.caretModel.moveToOffset(offset) }

    /** Anything the Brain does to the editor's caret or selection, on the EDT, is marked so it is not taken for the developer's. */
    fun <T> byBrain(block: () -> T): T {
        brainMoves++
        try { return block() } finally { brainMoves-- }
    }

    /**
     * The Sessions that have this buffer open. A Mirror belongs to the project,
     * but a Session's claim on it must not outlive the Session, and must not be
     * ended by another Session's didClose.
     */
    val owners: MutableSet<Any> = ConcurrentHashMap.newKeySet()
}

/**
 * The Mirror Set of SPEC.md §5.2. The Editor decides membership (didOpen /
 * didClose); this class owns what SPEC.md calls eviction: IntelliJ's own Editor
 * Tabs limit closes least-recently-used tabs, and a Mirror must never be lost
 * without the Bridge knowing. Spike Q6 found that pinned tabs are exempt and
 * that every closure arrives as fileClosed, so Mirrors are pinned, and any
 * closure we did not ask for is counted and undone.
 */
class MirrorSet(private val project: Project) : Disposable {

    private val log = logger<MirrorSet>()
    private val mirrors = ConcurrentHashMap<String, Mirror>()

    /** An edit made in the IDE window is forwarded to the Editor (ADR-0009). */
    val foreign = ForeignEdits(this)
    private val closingByUs = ConcurrentHashMap.newKeySet<VirtualFile>()
    val evictions = AtomicInteger()

    init {
        project.messageBus.connect(this).subscribe(
            FileEditorManagerListener.FILE_EDITOR_MANAGER,
            object : FileEditorManagerListener {
                override fun fileClosed(source: FileEditorManager, file: VirtualFile) {
                    if (file in closingByUs) return
                    val lost = mirrors.values.firstOrNull { it.file == file } ?: return
                    evictions.incrementAndGet()
                    log.warn("bridge: Mirror for ${lost.uri} was closed by IntelliJ; reopening")
                    ApplicationManager.getApplication().invokeLater { reopen(lost) }
                }
            },
        )
    }

    fun get(uri: String): Mirror? = mirrors[uri]
    fun all(): List<Mirror> = mirrors.values.sortedBy { it.uri }
    fun byFile(file: VirtualFile): Mirror? = mirrors.values.firstOrNull { it.file == file }

    /**
     * The Editor's active buffer changed. Make its Mirror the selected tab: the
     * daemon only analyses editors that are showing, and completion needs one.
     * IDE-internal; it does not take OS focus from the developer's terminal.
     */
    fun select(uri: String) {
        val m = mirrors[uri] ?: return
        edt { FileEditorManager.getInstance(project).openFile(m.file, false) }
    }

    /** One at a time: two Editors opening the same file together must end with one Mirror, not two on one Document. */
    @Synchronized
    fun open(uri: String, version: Int, rawText: String, owner: Any) {
        val text = normalise(rawText)
        mirrors[uri]?.let { existing ->
            // Already Mirrored by another Session (or this one, reattaching): take a
            // claim on it and leave the text alone. The first editor's unsaved
            // buffer is what the Mirror holds; a second editor connecting must not
            // overwrite it, and nothing could put it back. Two editors editing
            // one file at once is not supported.
            existing.owners += owner
            return
        }
        val file = resolve(uri) ?: throw IllegalArgumentException("no file for $uri")
        val mirror = edt {
            val editor = openEditor(file)
            val doc = editor.document
            // Mirrors are never edited by a person, so their history is noise
            // and would pin every intermediate state in the undo stack.
            UndoUtil.disableUndoFor(doc)
            // didOpen carries the buffer's text, which may differ from disk. Per
            // LSP the Brain must not read disk for an open document.
            if (doc.charsSequence.toString() != text) {
                WriteCommandAction.runWriteCommandAction(project) { doc.setText(text) }
            }
            PsiDocumentManager.getInstance(project).commitDocument(doc)
            Mirror(uri, file, doc, editor, version)
        }
        mirror.owners += owner
        MirroredFiles.add(file)
        mirrors[uri] = mirror
        edt { foreign.watch(mirror) }
        runCatching { onOpened?.invoke(mirror) }   // a watcher failing must never fail an open
    }

    /**
     * Neovim sends a `fileformat=dos` buffer with `\r\n` line endings, and IntelliJ
     * documents accept only `\n`: setText throws on anything else, and the Mirror is
     * never made. Line endings are a property of the file on disk, which the Editor
     * writes; the Mirror holds the logical text.
     */
    private fun normalise(text: String): String =
        if (text.indexOf('\r') < 0) text else text.replace("\r\n", "\n").replace('\r', '\n')

    fun change(uri: String, version: Int, changes: JsonArray) {
        val m = mirrors[uri] ?: throw IllegalArgumentException("not mirrored: $uri")
        m.convergent = false
        // An edit made in the IDE that this change is the echo of: the Mirror goes back to before it first.
        val echo = foreign.echoArrives(m)
        edt {
            m.applying = true
            try {
                WriteCommandAction.runWriteCommandAction(project) {
                    for (change in changes) apply(m.document, change.jsonObject)
                }
                PsiDocumentManager.getInstance(project).commitDocument(m.document)
            } finally {
                m.applying = false
            }
        }
        m.version = version
        m.convergent = true
        echo?.let { foreign.echoed(m, it) }
    }

    /**
     * The Editor has written the file (SPEC.md §5.4). The bytes on disk equal the
     * Mirror's, and IntelliJ's own reaction to the write is vetoed
     * (MirrorVetoer), so there is nothing to do but record the version. In
     * particular the Brain must not refresh or reload here: doing so raised the
     * modal "changes in memory and on disk" dialog and held the EDT.
     */
    fun saved(uri: String, version: Int) {
        val m = mirrors[uri] ?: return
        if (version >= 0) m.version = version
        m.convergent = true
        // Neovim has just written the file. An IDE that is not the active application does not
        // look at the disk by itself, and a file watcher may not be running, so its cached view of
        // the file is what it was before the write - and it is what a released Mirror reloads to,
        // which read as the file having lost its edits. Look now: one file. The veto stays, so the
        // Mirror's own text is not reloaded, only what the IDE believes the disk holds.
        com.intellij.openapi.vfs.VfsUtil.markDirtyAndRefresh(false, false, false, m.file)
    }

    /** A Session closed its buffer. The Mirror goes only when no Session has it open. */
    fun close(uri: String, owner: Any) {
        val m = mirrors[uri] ?: return
        m.owners -= owner
        if (m.owners.isEmpty()) release(uri)
    }

    /**
     * A Session went away (Neovim quit or crashed, or the socket dropped): take
     * back its claims. Returns the URIs of Mirrors that were released, so their
     * diagnostics can be cleared.
     */
    fun dropOwner(owner: Any): List<String> {
        val released = ArrayList<String>()
        for (m in mirrors.values.toList()) {
            if (m.owners.remove(owner) && m.owners.isEmpty()) {
                release(m.uri)
                released += m.uri
            }
        }
        return released
    }

    /** Called when a Mirror gets an editor, and again when it gets a new one (a tab was evicted). */
    @Volatile var onOpened: ((Mirror) -> Unit)? = null

    /** Called when a Mirror is released. */
    @Volatile var onReleased: ((String) -> Unit)? = null

    private fun release(uri: String) {
        val m = mirrors.remove(uri) ?: return
        edt { foreign.unwatch(m) }
        onReleased?.invoke(uri)
        closingByUs += m.file
        try {
            edt {
                // A released Mirror must not leave its text behind as an unsaved
                // document: once the veto lifts, IntelliJ would autosave a buffer
                // the developer may have discarded (:bd!) onto their file.
                MirroredFiles.allowingReload {
                    ApplicationManager.getApplication().runWriteAction {
                        FileDocumentManager.getInstance().reloadFromDisk(m.document)
                    }
                }
                MirroredFiles.remove(m.file)
                FileEditorManager.getInstance(project).closeFile(m.file)
            }
        } finally {
            closingByUs -= m.file
        }
    }

    fun isOpen(m: Mirror): Boolean = edt { FileEditorManager.getInstance(project).isFileOpen(m.file) }

    fun text(uri: String): String? = mirrors[uri]?.let { edt { it.document.charsSequence.toString() } }

    private fun openEditor(file: VirtualFile): Editor {
        val editor = FileEditorManager.getInstance(project)
            .openTextEditor(OpenFileDescriptor(project, file, 0), /* focusEditor = */ false)
            ?: error("openTextEditor returned null for ${file.path}")
        try {
            FileEditorManagerEx.getInstanceEx(project).currentWindow?.setFilePinned(file, true)
        } catch (t: Throwable) {
            log.warn("bridge: could not pin ${file.name}; it is exposed to tab eviction", t)
        }
        return editor
    }

    private fun reopen(lost: Mirror) {
        if (mirrors[lost.uri] !== lost) return
        lost.editor = openEditor(lost.file)
        runCatching { onOpened?.invoke(lost) }
    }

    private fun resolve(uri: String): VirtualFile? =
        LocalFileSystem.getInstance().refreshAndFindFileByPath(Path.of(URI(uri)).toString())

    private fun apply(doc: Document, change: JsonObject) {
        val text = normalise(change["text"]?.jsonPrimitive?.contentOrNull ?: "")
        val range = change["range"]?.jsonObject
        if (range == null) {
            doc.setText(text)
            return
        }
        val start = offset(doc, range["start"]!!.jsonObject)
        val end = offset(doc, range["end"]!!.jsonObject)
        doc.replaceString(start, end, text)
    }

    /**
     * For tests: change a Mirror's text as a person typing in the IDE window would, which is not the Brain's own
     * change and so is what [foreign] must notice. `remove` characters at line/character are replaced by `text`.
     */
    fun debugEdit(params: JsonObject) {
        val m = mirrors[params["uri"]!!.jsonPrimitive.content] ?: throw IllegalArgumentException("not mirrored")
        val remove = params["remove"]?.jsonPrimitive?.intOrNull ?: 0
        val text = params["text"]?.jsonPrimitive?.contentOrNull ?: ""
        edt {
            WriteCommandAction.runWriteCommandAction(project) {
                val at = offset(m.document, params)
                m.document.replaceString(at, minOf(at + remove, m.document.textLength), text)
            }
        }
    }

    override fun dispose() {
        foreign.dispose()
        mirrors.values.forEach { MirroredFiles.remove(it.file) }
        mirrors.clear()
    }

    companion object {
        /** LSP positions are line + UTF-16 code unit, which is what Document uses. */
        fun offset(doc: Document, position: JsonObject): Int {
            val line = position["line"]!!.jsonPrimitive.intOrNull ?: 0
            val character = position["character"]!!.jsonPrimitive.intOrNull ?: 0
            if (line >= doc.lineCount) return doc.textLength
            val start = doc.getLineStartOffset(line)
            return minOf(start + character, doc.getLineEndOffset(line))
        }
    }
}
