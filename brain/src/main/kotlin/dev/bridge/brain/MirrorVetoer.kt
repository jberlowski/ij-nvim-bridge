package dev.bridge.brain

import com.intellij.openapi.editor.Document
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.Disposable
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.fileEditor.FileDocumentSynchronizationVetoer
import com.intellij.openapi.fileEditor.impl.FileDocumentManagerImpl
import com.intellij.openapi.fileEditor.impl.MemoryDiskConflictResolver
import com.intellij.openapi.vfs.VirtualFile
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/** Every file currently Mirrored by any project in this IDE. */
object MirroredFiles {
    private val files = ConcurrentHashMap.newKeySet<VirtualFile>()
    fun add(file: VirtualFile) = files.add(file)
    fun remove(file: VirtualFile) = files.remove(file)
    fun contains(file: VirtualFile?) = file != null && file in files

    private val reloading = AtomicInteger()

    /** Lets the Brain's own reload through the veto, and nobody else's. */
    fun <T> allowingReload(block: () -> T): T {
        reloading.incrementAndGet()
        try { return block() } finally { reloading.decrementAndGet() }
    }

    fun reloadAllowed() = reloading.get() > 0
}

/**
 * Keeps IntelliJ's own disk synchronisation away from Mirrors (ADR-0003: the
 * Editor owns the bytes on disk, the Brain never writes).
 *
 * Without this two things go wrong, both found by the harness:
 *
 * - **Autosave writes.** IntelliJ saves unsaved documents on frame deactivation
 *   and when idle. A Mirror holds the buffer's unsaved text, so the IDE would
 *   silently write an Editor buffer the developer never saved.
 * - **A modal dialog blocks the EDT.** When the Editor writes the file while the
 *   Mirror is unsaved, IntelliJ sees a document changed "in memory and on disk"
 *   and asks the developer to choose. The dialog is modal, the EDT is held, and
 *   every Brain request that touches it hangs - it presents as a dead Brain.
 */
class MirrorVetoer : FileDocumentSynchronizationVetoer() {
    override fun maySaveDocument(document: Document, isSaveExplicit: Boolean): Boolean =
        !MirroredFiles.contains(FileDocumentManager.getInstance().getFile(document))

    override fun mayReloadFileContent(file: VirtualFile, document: Document): Boolean =
        !MirroredFiles.contains(file) || MirroredFiles.reloadAllowed()
}

/**
 * The veto above stops IntelliJ *acting* on a Mirror; this stops it *asking*.
 *
 * Whenever a file changes on disk under an unsaved document, IntelliJ records a
 * conflict and, when it next processes conflicts (on application activation or a
 * save), raises the modal "changes have been made in memory and on disk" dialog.
 * The EDT is then held until someone clicks, and no Brain request that touches
 * it can complete. For a Mirror the answer is always "keep memory": the Editor's
 * buffer is the Source of Truth, and the disk change is the Editor's own write.
 *
 * FileDocumentManagerImpl exposes this hook publicly, but it is an impl class:
 * expect to revisit it on a new IntelliJ major (ADR-0007).
 */
class MirrorConflictResolver : MemoryDiskConflictResolver() {
    override fun askReloadFromDisk(file: VirtualFile, document: Document): Boolean =
        if (MirroredFiles.contains(file)) false else super.askReloadFromDisk(file, document)
}

/** Owns the resolver's lifetime: one per IDE, however many projects are open. */
@Service(Service.Level.APP)
class BridgeApp : Disposable {
    @Volatile private var installed = false

    @Synchronized
    fun install() {
        if (installed) return
        (FileDocumentManager.getInstance() as? FileDocumentManagerImpl)
            ?.setAskReloadFromDisk(this, MirrorConflictResolver())
        installed = true
    }

    override fun dispose() {}
}
