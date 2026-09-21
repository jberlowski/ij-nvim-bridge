package dev.bridge.brain

import com.intellij.codeInsight.TargetElementUtil
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import com.intellij.openapi.roots.ProjectFileIndex
import com.intellij.openapi.util.TextRange
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiNameIdentifierOwner
import com.intellij.psi.PsiNamedElement
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.refactoring.rename.RenamePsiElementProcessor
import com.intellij.refactoring.rename.RenameUtil
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.nio.file.Path

/**
 * `textDocument/prepareRename` and `textDocument/rename` (FEATURES.md §3).
 *
 * Data first (D2): IntelliJ is asked what it would rename - the element, what
 * must be renamed with it (overriders, a Java class's file) and every usage - and
 * the answer becomes a `WorkspaceEdit` for Neovim to apply. Nothing is changed in
 * IntelliJ, which is what makes it safe to answer from the developer's unsaved
 * buffers. It works on IntelliJ's read-only view (`RenameUtil.findUsages`), so
 * it runs in a background read action like navigation does.
 *
 * What it does not do, yet: IntelliJ's own rename asks, in a dialog, whether to
 * rename the base method when asked to rename an override, and what to do about
 * name conflicts. There is no one to ask, so an override is renamed with its own
 * overriders and usages, and conflicts are not checked.
 */
class Rename(private val project: Project, private val locations: Locations) {

    /** The request cannot be honoured, and the developer should be told why. */
    class Refused(message: String) : RuntimeException(message)

    private class Site(val element: PsiElement, val range: TextRange)

    private fun site(editor: Editor, file: PsiFile, offset: Int): Site? {
        val utils = TargetElementUtil.getInstance()
        val element = utils.findTargetElement(editor, utils.allAccepted, offset) ?: return null
        if (element !is PsiNamedElement) return null

        // The name as written at the caret: a reference, or the declaration itself.
        val reference = file.findReferenceAt(offset)
        val range = reference?.takeIf { it.isReferenceTo(element) }
            ?.let { it.rangeInElement.shiftRight(it.element.textRange.startOffset) }
            ?: (element as? PsiNameIdentifierOwner)?.nameIdentifier?.textRange
        if (range == null || range.isEmpty || offset < range.startOffset || offset > range.endOffset) return null
        return Site(element, range)
    }

    private fun checkRenamable(element: PsiElement) {
        val virtual = element.containingFile?.originalFile?.virtualFile
        if (virtual == null || !ProjectFileIndex.getInstance(project).isInContent(virtual)) {
            throw Refused("'${(element as? PsiNamedElement)?.name}' is not part of this project, so it cannot be renamed")
        }
    }

    // ------------------------------------------------------------ prepareRename
    /** Must run in a read action. Null when there is nothing renamable at the caret. */
    fun prepare(editor: Editor, file: PsiFile, offset: Int): JsonElement {
        val site = site(editor, file, offset) ?: return JsonNull
        checkRenamable(site.element)
        val doc = editor.document
        return buildJsonObject {
            put("range", buildJsonObject {
                put("start", Locations.position(doc, site.range.startOffset))
                put("end", Locations.position(doc, site.range.endOffset))
            })
            put("placeholder", doc.getText(site.range))
        }
    }

    // ------------------------------------------------------------------- rename
    /**
     * Not in a read action: three phases, since IntelliJ's own steps disagree about where
     * they may run. Working out what else must be renamed (overriders) can use a modal
     * progress, which cannot be waited for from a read action, so it runs on the EDT, as
     * IntelliJ's own Rename does; finding the usages is a read action.
     */
    fun rename(mirror: Mirror, params: JsonObject): JsonElement {
        val newName = (params["newName"] as? JsonPrimitive)?.content ?: throw Refused("no new name given")
        val position = params["position"] as JsonObject

        val (element, allRenames) = ReadAction.compute<Pair<PsiElement, LinkedHashMap<PsiElement, String>>, RuntimeException> {
            val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document)
                ?: throw Refused("no file to rename in")
            val site = site(mirror.editor, file, MirrorSet.offset(mirror.document, position))
                ?: throw Refused("there is nothing to rename here")
            checkRenamable(site.element)
            if (!RenameUtil.isValidName(project, site.element, newName)) throw Refused("'$newName' is not a valid name here")
            site.element to linkedMapOf(site.element to newName)
        }

        // What must change with it: overriders, accessors.
        edt {
            RenamePsiElementProcessor.forElement(element)
                .prepareRenaming(element, newName, allRenames, GlobalSearchScope.projectScope(project))
        }

        return ReadAction.compute<JsonElement, RuntimeException> { workspaceEdit(allRenames, element, newName) }
    }

    /** A top-level class's file, when the file is named after it: Java requires it, and Kotlin does it for a lone class. */
    private fun fileNamedAfter(element: PsiElement, newName: String): Pair<PsiFile, String>? {
        val file = element.containingFile ?: return null
        val virtual = file.originalFile.virtualFile ?: return null
        if (element.parent != file || virtual.nameWithoutExtension != (element as? PsiNamedElement)?.name) return null
        if (file.language.id == "kotlin" && file.children.count { it is PsiNamedElement } != 1) return null
        return file to (virtual.extension?.let { "$newName.$it" } ?: newName)
    }

    private fun workspaceEdit(allRenames: LinkedHashMap<PsiElement, String>, element: PsiElement, newName: String): JsonElement {
        val edits = LinkedHashMap<String, MutableList<Pair<JsonObject, String>>>() // uri -> (range, new text)
        val seen = HashSet<String>()
        fun add(file: PsiFile, range: TextRange, text: String) {
            val at = locations.locate(file, range) ?: return
            val uri = at["uri"].let { (it as JsonPrimitive).content }
            val where = at["range"] as JsonObject
            if (seen.add("$uri$where")) edits.getOrPut(uri) { ArrayList() } += where to text
        }

        val renamedFiles = ArrayList<Pair<PsiFile, String>>()
        for ((target, name) in allRenames.toMap()) {
            fileNamedAfter(target, name)?.let { if (renamedFiles.none { r -> r.first == it.first }) renamedFiles += it }
            if (target is PsiFile) {
                renamedFiles += target to name
                continue
            }
            (target as? PsiNameIdentifierOwner)?.nameIdentifier?.let { id ->
                id.containingFile?.let { add(it, id.textRange, name) }
            }
            for (usage in RenameUtil.findUsages(target, name, false, false, allRenames)) {
                if (usage.isNonCodeUsage) continue
                val e = usage.element ?: continue
                val containing = e.containingFile ?: continue
                val inside = usage.rangeInElement ?: TextRange(0, e.textLength)
                add(containing, inside.shiftRight(e.textRange.startOffset), name)
            }
        }

        val textEdits = edits.mapValues { (_, list) ->
            JsonArray(list.sortedWith(compareBy({ position(it.first, "start", "line") }, { position(it.first, "start", "character") }))
                .map { (range, text) -> buildJsonObject { put("range", range); put("newText", text) } })
        }
        if (renamedFiles.isEmpty()) {
            return buildJsonObject {
                put("changes", buildJsonObject { textEdits.forEach { (uri, list) -> put(uri, list) } })
            }
        }
        // A file that changes name goes in `documentChanges`: the edits first, at the old
        // path, and then the rename.
        return buildJsonObject {
            put("documentChanges", JsonArray(
                textEdits.map { (uri, list) ->
                    buildJsonObject {
                        put("textDocument", buildJsonObject { put("uri", uri); put("version", JsonNull) })
                        put("edits", list)
                    }
                } + renamedFiles.mapNotNull { (f, name) ->
                    val old = (f.originalFile.virtualFile ?: return@mapNotNull null).path
                    buildJsonObject {
                        put("kind", "rename")
                        put("oldUri", Path.of(old).toUri().toString())
                        put("newUri", Path.of(old).resolveSibling(name).toUri().toString())
                    }
                }))
        }
    }

    private fun position(range: JsonObject, end: String, field: String): Int =
        ((range[end] as JsonObject)[field] as JsonPrimitive).content.toInt()
}
