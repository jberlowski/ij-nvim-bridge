package dev.bridge.brain

import com.intellij.openapi.editor.Document
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.TextRange
import com.intellij.openapi.util.text.StringUtil
import com.intellij.psi.PsiCompiledFile
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiNameIdentifierOwner
import com.intellij.psi.PsiDocumentManager
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest

/**
 * Turning IntelliJ PSI into LSP locations (FEATURES.md §2).
 *
 * Positions come from the file's *document*, so a target in a buffer the
 * Editor has unsaved changes for is reported on the line the Editor sees, not
 * the line disk has. A target in library code is not a file Neovim can open:
 * per D1 its source (or IntelliJ's decompiled text) is written to a read-only
 * file in a cache directory and reported as a `file://` location. Everything
 * that needs a Location goes through [of], so the extraction can later be
 * replaced by a virtual document in one place.
 */
class Locations(private val project: Project) {

    /** Location of the element's *name*, or of the whole element when it has none. */
    fun of(element: PsiElement): JsonObject? {
        val target = element.navigationElement ?: element
        val file = target.containingFile ?: return null
        val range = nameRange(target) ?: return null
        return locate(file, range)
    }

    /** Location of `range` (absolute offsets in the file's text) in [file]. */
    fun locate(file: PsiFile, range: TextRange): JsonObject? {
        val virtual = (file.originalFile.virtualFile ?: file.virtualFile) ?: return null
        val document = PsiDocumentManager.getInstance(project).getDocument(file)
        val uri: String
        val start: JsonObject
        val end: JsonObject
        if (virtual.isInLocalFileSystem && document != null) {
            uri = Path.of(virtual.path).toUri().toString()
            start = position(document, range.startOffset)
            end = position(document, range.endOffset)
        } else {
            // Library code: from a jar, or decompiled. Not a file Neovim can open.
            val text = textOf(file)
            uri = extract(virtual.url, virtual.name, text)
            start = position(text, range.startOffset)
            end = position(text, range.endOffset)
        }
        return buildJsonObject {
            put("uri", uri)
            put("range", buildJsonObject { put("start", start); put("end", end) })
        }
    }

    private fun nameRange(element: PsiElement): TextRange? =
        (element as? PsiNameIdentifierOwner)?.nameIdentifier?.textRange?.takeIf { !it.isEmpty }
            ?: element.textRange

    /** Decompiled text for a compiled class; the file's own text otherwise. */
    private fun textOf(file: PsiFile): String =
        if (file is PsiCompiledFile) file.mirror.text else file.text

    /**
     * D1: extract library source to the developer's own cache directory,
     * read-only, keyed by the library entry so a changed jar gets a new path.
     */
    private fun extract(url: String, name: String, text: String): String {
        val key = MessageDigest.getInstance("SHA-256").digest(url.toByteArray()).take(6)
            .joinToString("") { "%02x".format(it) }
        val fileName = if (name.endsWith(".class")) name.removeSuffix(".class") + ".java" else name
        val dir = cacheDir().resolve(key)
        val path = dir.resolve(fileName)
        if (!Files.exists(path) || Files.readString(path) != text) {
            Files.createDirectories(dir)
            Files.deleteIfExists(path)
            Files.writeString(path, text)
            Files.setPosixFilePermissions(path, PosixFilePermissions.fromString("r--r--r--"))
        }
        return path.toUri().toString()
    }

    private fun cacheDir(): Path {
        val base = System.getenv("XDG_CACHE_HOME")?.takeIf { it.isNotBlank() }
            ?: (System.getProperty("user.home") + "/.cache")
        return Path.of(base, "ij-nvim-bridge", "library")
    }

    companion object {
        fun position(doc: Document, offset: Int): JsonObject {
            if (doc.textLength == 0) return pos(0, 0)
            val o = offset.coerceIn(0, doc.textLength)
            val line = doc.getLineNumber(o)
            return pos(line, o - doc.getLineStartOffset(line)) // UTF-16, as Document counts
        }

        fun position(text: String, offset: Int): JsonObject {
            val lc = StringUtil.offsetToLineColumn(text, offset.coerceIn(0, text.length))
            return pos(lc.line, lc.column)
        }

        private fun pos(line: Int, character: Int) = buildJsonObject {
            put("line", line)
            put("character", character)
        }
    }
}
