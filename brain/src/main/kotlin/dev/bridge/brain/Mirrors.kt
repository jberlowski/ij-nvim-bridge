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

    fun open(uri: String, version: Int, text: String) {
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
        MirroredFiles.add(file)
        mirrors[uri] = mirror
    }

    fun change(uri: String, version: Int, changes: JsonArray) {
        val m = mirrors[uri] ?: throw IllegalArgumentException("not mirrored: $uri")
        m.convergent = false
        edt {
            WriteCommandAction.runWriteCommandAction(project) {
                for (change in changes) apply(m.document, change.jsonObject)
            }
            PsiDocumentManager.getInstance(project).commitDocument(m.document)
        }
        m.version = version
        m.convergent = true
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
    }

    fun close(uri: String) {
        val m = mirrors.remove(uri) ?: return
        closingByUs += m.file
        try {
            edt {
                // A closed Mirror must not leave its text behind as an unsaved
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
    }

    private fun resolve(uri: String): VirtualFile? =
        LocalFileSystem.getInstance().refreshAndFindFileByPath(Path.of(URI(uri)).toString())

    private fun apply(doc: Document, change: JsonObject) {
        val text = change["text"]?.jsonPrimitive?.contentOrNull ?: ""
        val range = change["range"]?.jsonObject
        if (range == null) {
            doc.setText(text)
            return
        }
        val start = offset(doc, range["start"]!!.jsonObject)
        val end = offset(doc, range["end"]!!.jsonObject)
        doc.replaceString(start, end, text)
    }

    override fun dispose() {
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
