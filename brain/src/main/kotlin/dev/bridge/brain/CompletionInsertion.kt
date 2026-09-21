package dev.bridge.brain

import com.intellij.codeInsight.completion.CompletionInitializationContext
import com.intellij.codeInsight.completion.InsertionContext
import com.intellij.codeInsight.completion.OffsetMap
import com.intellij.codeInsight.lookup.Lookup
import com.intellij.codeInsight.lookup.LookupElement
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.editor.EditorFactory
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiFile
import com.intellij.application.options.CodeStyle
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** The elements completion answered with, by id, for a while: enough to cover the Editor's answer cache. */
class ElementStore {
    class Held(val element: LookupElement, val site: CompletionEngine.Site)

    private val map = object : LinkedHashMap<String, Held>(1024, 0.75f, false) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Held>?) = size > MAX
    }

    @Synchronized fun put(id: String, element: LookupElement, site: CompletionEngine.Site) {
        map[id] = Held(element, site)
    }

    @Synchronized fun get(id: String): Held? = map[id]

    companion object {
        const val MAX = 8000
    }
}

/**
 * `completionItem/resolve`: what accepting an item does, exactly as IntelliJ does
 * it (FEATURES.md §3): the import, the parentheses, the lambda braces.
 *
 * IntelliJ's own `LookupElement.handleInsert` is run, on a throwaway copy of the
 * file with a throwaway editor (D2): the Mirror and the disk are never touched.
 * The difference between the copy before and after becomes edits for Neovim to
 * apply: `textEdit` for the word being completed and whatever it grew into,
 * `additionalTextEdits` for everything elsewhere (imports), and, when IntelliJ
 * leaves the caret inside what it inserted (`foo(|)`), a snippet `$0`.
 *
 * The item is valid only for the text it was offered against: after any other
 * change to the buffer it is `ContentModified`, and the client inserts the plain word.
 */
class CompletionInsertion(private val project: Project, private val store: ElementStore) {

    fun resolve(mirror: Mirror, item: JsonObject): JsonElement {
        val id = item["data"]?.jsonObject?.get("id")?.jsonPrimitive?.contentOrNull
            ?: throw IllegalArgumentException("no data.id on the item")
        val held = store.get(id) ?: throw CodeActions.StaleAction()
        val doc = mirror.document
        val (original, file) = ReadAction.compute<Pair<String, PsiFile?>, RuntimeException> {
            doc.text to PsiDocumentManager.getInstance(project).getPsiFile(doc)
        }
        if (file == null) throw CodeActions.StaleAction()
        // Valid against the text it was offered for, and against that text with more of the
        // word typed at the caret since: an answer for `UUI` is right for `UUID` too. Any other
        // change, and the item no longer describes what IntelliJ would do.
        val at = caretNow(held.site, original) ?: throw CodeActions.StaleAction()
        val settings = ReadAction.compute<com.intellij.psi.codeStyle.CodeStyleSettings, RuntimeException> {
            CodeStyle.getSettings(file)
        }

        var start = at
        while (start > 0 && Character.isJavaIdentifierPart(original[start - 1])) start--
        var idEnd = at
        while (idEnd < original.length && Character.isJavaIdentifierPart(original[idEnd])) idEnd++

        var inserted = original
        var caret = at
        edt {
            WriteCommandAction.runWriteCommandAction(project) {
                val copy = file.copy() as PsiFile
                val copyDoc = PsiDocumentManager.getInstance(project).getDocument(copy)
                    ?: copy.viewProvider.document
                    ?: throw IllegalStateException("the copy has no document")
                check(copyDoc.text == original) { "the copy's document differs from the buffer" }
                val editor = EditorFactory.getInstance().createEditor(copyDoc, project)
                try {
                    CodeStyle.runWithLocalSettings(project, settings, Runnable {
                        val lookupString = held.element.lookupString
                        copyDoc.replaceString(start, at, lookupString)
                        editor.caretModel.moveToOffset(start + lookupString.length)
                        val offsets = OffsetMap(copyDoc)
                        offsets.addOffset(CompletionInitializationContext.START_OFFSET, start)
                        offsets.addOffset(CompletionInitializationContext.SELECTION_END_OFFSET, start + lookupString.length)
                        offsets.addOffset(CompletionInitializationContext.IDENTIFIER_END_OFFSET,
                            start + lookupString.length + (idEnd - at))
                        val psi = PsiDocumentManager.getInstance(project)
                        psi.commitDocument(copyDoc)
                        val context = InsertionContext(offsets, Lookup.NORMAL_SELECT_CHAR,
                            arrayOf<LookupElement>(held.element), copy, editor, false)
                        held.element.handleInsert(context)
                        psi.doPostponedOperationsAndUnblockDocument(copyDoc)
                        psi.commitDocument(copyDoc)
                    })
                    inserted = copyDoc.text
                    caret = editor.caretModel.offset
                } finally {
                    EditorFactory.getInstance().releaseEditor(editor)
                }
            }
        }
        return withEdits(item, original, inserted, start, at, caret)
    }

    /** Where the caret is now, if [now] is [Site.text] plus keyword characters typed at the caret; else null. */
    private fun caretNow(site: CompletionEngine.Site, now: String): Int? {
        val before = site.text
        val grown = now.length - before.length
        if (grown < 0 || site.offset > before.length) return null
        if (!now.startsWith(before.substring(0, site.offset)) || !now.endsWith(before.substring(site.offset))) return null
        val typed = now.substring(site.offset, site.offset + grown)
        return if (typed.all { Character.isJavaIdentifierPart(it) }) site.offset + grown else null
    }

    /**
     * Split the difference into the edit at the word being completed and the rest.
     * Edits are trimmed spans, and those that touch [start, at] are the main one.
     */
    private fun withEdits(item: JsonObject, before: String, after: String, start: Int, at: Int, caret: Int): JsonObject {
        val regions = TextEdits.regions(before, after)
        val touching = regions.filter { it.start1 <= at && it.end1 >= start }
        val others = regions - touching.toSet()
        val a = minOf(start, touching.minOfOrNull { it.start1 } ?: start)
        val b = maxOf(at, touching.maxOfOrNull { it.end1 } ?: at)
        fun growth(r: TextEdits.Region) = (r.end2 - r.start2) - (r.end1 - r.start1)
        val beforeWindow = others.filter { it.end1 <= a }.sumOf(::growth)
        val newA = a + beforeWindow
        val newB = b + beforeWindow + touching.sumOf(::growth)
        val main = after.substring(newA, newB)
        val rel = caret - newA
        val snippet = rel in 0 until main.length
        val text = if (snippet) escape(main.substring(0, rel)) + "\$0" + escape(main.substring(rel)) else main

        return JsonObject(item + mapOf(
            "textEdit" to TextEdits.edit(before, a, b, text),
            "additionalTextEdits" to JsonArray(others.map { TextEdits.edit(before, it.start1, it.end1, after.substring(it.start2, it.end2)) }),
            "insertTextFormat" to JsonPrimitive(if (snippet) 2 else 1),
        ))
    }

    private fun escape(s: String) = s.replace("\\", "\\\\").replace("$", "\\$").replace("}", "\\}")
}
