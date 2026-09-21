package dev.bridge.brain

import com.intellij.diff.comparison.ComparisonManager
import com.intellij.diff.comparison.ComparisonPolicy
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.progress.EmptyProgressIndicator
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiFile
import com.intellij.application.options.CodeStyle
import com.intellij.psi.codeStyle.CodeStyleManager
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `textDocument/formatting` and `rangeFormatting`: the project's original
 * motivation. The developer's *IDE* code style - `.editorconfig`, the project's
 * scheme, the language's rules - applied to their buffer, not a headless
 * engine's idea of it (SPEC.md §1).
 *
 * Edits are computed, never applied to the Mirror (FEATURES.md §2, D2): the
 * Editor owns the bytes. So the file is copied, the copy is reformatted, and the
 * difference becomes `TextEdit[]` for Neovim to apply to its own buffer. The
 * Mirror and the disk are untouched by a request.
 *
 * The client's FormattingOptions (tabSize, insertSpaces) are deliberately
 * ignored: Neovim's settings are not the IDE's, and using the IDE's is the point.
 *
 * Reformatting is a write action and must run on the EDT, so this cannot share
 * the read-action path navigation uses: waiting for the EDT from inside a read
 * action can deadlock against a write waiting for that read to end.
 */
class Formatting(private val project: Project) {

    private val log = logger<Formatting>()

    /** @param range null formats the whole file. */
    fun edits(mirror: Mirror, range: JsonObject?): JsonElement {
        val doc = mirror.document
        val (original, file, bounds) = ReadAction.compute<Triple<String, PsiFile?, Pair<Int, Int>?>, RuntimeException> {
            Triple(
                doc.text,
                PsiDocumentManager.getInstance(project).getPsiFile(doc),
                range?.let {
                    MirrorSet.offset(doc, it["start"] as JsonObject) to MirrorSet.offset(doc, it["end"] as JsonObject)
                },
            )
        }
        if (file == null) return JsonArray(emptyList())

        // The style comes from the ORIGINAL file: `.editorconfig`, the project's
        // scheme, the language's rules. A copy is not on disk, so asked for its own
        // settings it can lose that context and quietly fall back to the defaults,
        // which is how a project's 2-space `.editorconfig` came out as 4 spaces.
        val settings = ReadAction.compute<com.intellij.psi.codeStyle.CodeStyleSettings, RuntimeException> {
            CodeStyle.getSettings(file)
        }
        val formatted = edt {
            var result = original
            WriteCommandAction.runWriteCommandAction(project) {
                val copy = file.copy() as PsiFile // a non-physical copy: no document, no disk
                val style = CodeStyleManager.getInstance(project)
                CodeStyle.runWithLocalSettings(project, settings, Runnable {
                    if (bounds == null) style.reformat(copy) else style.reformatRange(copy, bounds.first, bounds.second)
                })
                result = copy.text
            }
            result
        }
        return JsonArray(TextEdits.diff(original, formatted))
    }
}


/** Minimal line-based LSP `TextEdit[]` between two texts. Shared by every edit feature. */
object TextEdits {
    private val log = logger<TextEdits>()

    /** Minimal line-based edits turning [a] into [b]. */
    fun diff(a: String, b: String): List<JsonObject> {
        if (a == b) return emptyList()
        val fragments = try {
            ComparisonManager.getInstance().compareLines(a, b, ComparisonPolicy.DEFAULT, EmptyProgressIndicator())
        } catch (t: Throwable) {
            // Too large to diff: replace the whole text rather than fail.
            log.info("bridge: formatting diff fell back to a whole-file edit", t)
            return listOf(edit(a, 0, a.length, b))
        }
        val la = lineStarts(a)
        val lb = lineStarts(b)
        return fragments.map { f ->
            val start1 = la.getOrElse(f.startLine1) { a.length }
            val end1 = la.getOrElse(f.endLine1) { a.length }
            val start2 = lb.getOrElse(f.startLine2) { b.length }
            val end2 = lb.getOrElse(f.endLine2) { b.length }
            edit(a, start1, end1, b.substring(start2, end2))
        }
    }

    private fun lineStarts(text: String): List<Int> {
        val out = ArrayList<Int>()
        out += 0
        text.forEachIndexed { i, c -> if (c == '\n') out += i + 1 }
        return out
    }

    /** A changed span: [start1, end1) of the old text became [start2, end2) of the new. */
    class Region(val start1: Int, val end1: Int, val start2: Int, val end2: Int)

    /** The changed spans between [a] and [b], each trimmed of the text it shares at either end. */
    fun regions(a: String, b: String): List<Region> {
        if (a == b) return emptyList()
        val fragments = try {
            ComparisonManager.getInstance().compareLines(a, b, ComparisonPolicy.DEFAULT, EmptyProgressIndicator())
        } catch (t: Throwable) {
            listOf(null)
        }
        val la = lineStarts(a)
        val lb = lineStarts(b)
        return fragments.map { f ->
            var s1 = if (f == null) 0 else la.getOrElse(f.startLine1) { a.length }
            var e1 = if (f == null) a.length else la.getOrElse(f.endLine1) { a.length }
            var s2 = if (f == null) 0 else lb.getOrElse(f.startLine2) { b.length }
            var e2 = if (f == null) b.length else lb.getOrElse(f.endLine2) { b.length }
            while (s1 < e1 && s2 < e2 && a[s1] == b[s2]) { s1++; s2++ }
            while (e1 > s1 && e2 > s2 && a[e1 - 1] == b[e2 - 1]) { e1--; e2-- }
            Region(s1, e1, s2, e2)
        }
    }

    fun edit(text: String, start: Int, end: Int, newText: String) = buildJsonObject {
        put("range", buildJsonObject {
            put("start", Locations.position(text, start))
            put("end", Locations.position(text, end))
        })
        put("newText", newText)
    }
}
