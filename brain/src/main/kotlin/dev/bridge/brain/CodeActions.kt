package dev.bridge.brain

import com.intellij.application.options.CodeStyle
import com.intellij.codeInsight.daemon.impl.ShowIntentionsPass
import com.intellij.modcommand.ActionContext
import com.intellij.modcommand.ModCommand
import com.intellij.modcommand.ModCompositeCommand
import com.intellij.modcommand.ModUpdateFileText
import com.intellij.lang.LanguageImportStatements
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiFile
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * `textDocument/codeAction` and `codeAction/resolve`, so far offering one action:
 * organize imports (`source.organizeImports`).
 *
 * D3: titles now, edits on resolve. Listing is cheap and answers from the PSI;
 * the edit needs the import optimiser to resolve references, so it is computed
 * only when the developer picks the action. D2: the edit is computed on a copy of
 * the file and diffed, never applied to the Mirror. The action carries the
 * Mirror's version, and resolving against a changed Mirror is `ContentModified`,
 * since imports computed for old text would be wrong for the new.
 */
class CodeActions(private val project: Project) {

    companion object {
        const val ORGANIZE_IMPORTS = "source.organizeImports"
        const val GENERATE = "source.generate"
        const val QUICK_FIX = "quickfix"
        const val REWRITE = "refactor.rewrite"
        val KINDS = listOf(ORGANIZE_IMPORTS, GENERATE, QUICK_FIX, REWRITE)
    }

    // ------------------------------------------------------------------ listing
    /** Must run in a read action. */
    fun list(mirror: Mirror, params: JsonObject): JsonElement {
        val only = params["context"]?.jsonObject?.get("only")?.jsonArray
            ?.mapNotNull { it.jsonPrimitive.contentOrNull }
        val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document)
            ?: return JsonArray(emptyList())

        val actions = ArrayList<JsonObject>()
        if (wanted(ORGANIZE_IMPORTS, only) && supportsImports(file)) {
            actions += buildJsonObject {
                put("title", "Organize imports")
                put("kind", ORGANIZE_IMPORTS)
                // Everything resolve needs, and nothing computed yet.
                put("data", buildJsonObject {
                    put("uri", mirror.uri)
                    put("version", mirror.version)
                    put("kind", ORGANIZE_IMPORTS)
                })
            }
        }
        actions += generateAt(mirror, file, params, only)
        actions += intentionsAt(mirror, params, only)
        return JsonArray(actions)
    }

    // ---------------------------------------------------- quick fixes and intentions
    /** One thing IntelliJ offers at a place: its title, and where it came from. */
    private class Offer(val kind: String, val key: String, val title: String, val action: com.intellij.modcommand.ModCommandAction)

    /**
     * What IntelliJ would show under Alt+Enter at [start]-[end]: the fixes for the errors and warnings
     * there, and the intentions. Only those that have a `ModCommand` form are offered: that is an
     * action's effect as *data*, which is what lets the Brain answer with edits and touch nothing. An
     * older action mutates the file when invoked, and cannot be run against a copy safely (its
     * pointers are into the original). Must run in a read action, with the Mirror's caret and
     * selection already at the range (the engine does that first).
     */
    private fun offersAt(mirror: Mirror): List<Offer> {
        val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document) ?: return emptyList()
        val info = ShowIntentionsPass.getActionsToShow(mirror.editor, file)
        val context = ActionContext.from(mirror.editor, file)
        val out = LinkedHashMap<String, Offer>()
        val groups = listOf(
            info.errorFixesToShow to QUICK_FIX, info.inspectionFixesToShow to QUICK_FIX,
            info.intentionsToShow to REWRITE,
        )
        for ((descriptors, kind) in groups) {
            for (descriptor in descriptors) {
                val intention = descriptor.action
                val mod = intention.asModCommandAction() ?: continue
                val presentation = mod.getPresentation(context) ?: continue
                val title = presentation.name.ifBlank { intention.text }
                val key = "${intention.familyName}|$title"
                out.putIfAbsent(key, Offer(kind, key, title, mod))
            }
        }
        return out.values.toList()
    }

    /** What the Generate menu would offer for the class at the range: constructors, accessors, `toString`, ... */
    private fun generateAt(mirror: Mirror, file: PsiFile, params: JsonObject, only: List<String>?): List<JsonObject> {
        val range = params["range"]?.jsonObject ?: return emptyList()
        val generator = Generator.forFile(file) ?: return emptyList()
        val offset = MirrorSet.offset(mirror.document, range["start"]!!.jsonObject)
        return generator.offers(file, offset).filter { wanted(it.kind, only) }.map { offer ->
            buildJsonObject {
                put("title", offer.title)
                put("kind", offer.kind)
                put("data", buildJsonObject {
                    put("uri", mirror.uri)
                    put("version", mirror.version)
                    put("kind", offer.kind)
                    put("key", offer.key)
                    put("range", range)
                })
            }
        }
    }

    private fun intentionsAt(mirror: Mirror, params: JsonObject, only: List<String>?): List<JsonObject> {
        val range = params["range"]?.jsonObject ?: return emptyList()
        if (only != null && only.isNotEmpty() && only.none { QUICK_FIX.startsWith(it) || REWRITE.startsWith(it) || it == "quickfix" }) {
            return emptyList()
        }
        return offersAt(mirror).filter { wanted(it.kind, only) }.map { offer ->
            buildJsonObject {
                put("title", offer.title)
                put("kind", offer.kind)
                if (offer.kind == QUICK_FIX) put("isPreferred", false)
                put("data", buildJsonObject {
                    put("uri", mirror.uri)
                    put("version", mirror.version)
                    put("kind", offer.kind)
                    put("key", offer.key)
                    put("range", range)
                })
            }
        }
    }

    /** LSP `only` filtering: a requested kind matches itself and anything under it. */
    private fun wanted(kind: String, only: List<String>?): Boolean =
        only.isNullOrEmpty() || only.any { kind == it || kind.startsWith("$it.") }

    private fun supportsImports(file: PsiFile) =
        LanguageImportStatements.INSTANCE.forFile(file).any { it.supports(file) }

    // ----------------------------------------------------------------- resolve
    /** Not in a read action: it needs a write action on the EDT (on a copy). */
    fun resolve(mirror: Mirror, action: JsonObject): JsonElement {
        val data = action["data"]?.jsonObject ?: throw IllegalArgumentException("no data on the action")
        val version = data["version"]?.jsonPrimitive?.intOrNull
        if (version != null && version != mirror.version) {
            throw StaleAction()
        }
        val kind = data["kind"]?.jsonPrimitive?.contentOrNull
        if (kind == QUICK_FIX || kind == REWRITE) return resolveIntention(mirror, action, data, version)
        if (kind != null && kind.startsWith(GENERATE)) return resolveGenerate(mirror, action, data, version)
        val edits = when (kind) {
            ORGANIZE_IMPORTS -> organizeImports(mirror)
            else -> throw IllegalArgumentException("unknown action kind")
        }
        if (mirror.version != version && version != null) throw StaleAction()
        return buildJsonObject {
            put("title", action["title"] ?: JsonPrimitive("Organize imports"))
            put("kind", action["kind"] ?: JsonPrimitive(ORGANIZE_IMPORTS))
            put("edit", buildJsonObject {
                put("changes", buildJsonObject { put(mirror.uri, JsonArray(edits)) })
            })
        }
    }

    /**
     * Generated on a copy of the file, in a write action on the EDT, then diffed: the Mirror and the disk
     * are never touched (D2). The style is the original file's, as for formatting.
     */
    private fun resolveGenerate(mirror: Mirror, action: JsonObject, data: JsonObject, version: Int?): JsonElement {
        val key = data["key"]?.jsonPrimitive?.contentOrNull ?: throw IllegalArgumentException("no key on the action")
        val range = data["range"]?.jsonObject ?: throw IllegalArgumentException("no range on the action")
        val doc = mirror.document
        val (original, file, offset) = ReadAction.compute<Triple<String, PsiFile?, Int>, RuntimeException> {
            Triple(doc.text, PsiDocumentManager.getInstance(project).getPsiFile(doc), MirrorSet.offset(doc, range["start"]!!.jsonObject))
        }
        if (file == null) throw StaleAction()
        val generator = Generator.forFile(file) ?: throw StaleAction()
        val settings = ReadAction.compute<com.intellij.psi.codeStyle.CodeStyleSettings, RuntimeException> { CodeStyle.getSettings(file) }
        val generated = edt {
            var result = original
            val run = {
                val copy = file.copy() as PsiFile
                CodeStyle.runWithLocalSettings(project, settings, Runnable { generator.generate(copy, offset, key) })
                result = copy.text
            }
            if (generator.needsWriteAction(key)) WriteCommandAction.runWriteCommandAction(project) { run() } else run()
            result
        }
        if (mirror.version != version && version != null) throw StaleAction()
        return buildJsonObject {
            put("title", action["title"] ?: JsonPrimitive("Generate"))
            put("kind", action["kind"] ?: JsonPrimitive(GENERATE))
            put("edit", buildJsonObject {
                put("changes", buildJsonObject { put(mirror.uri, JsonArray(TextEdits.diff(original, generated))) })
            })
        }
    }

    /** The offered action is run for its `ModCommand`, and that command is read, never executed. */
    private fun resolveIntention(mirror: Mirror, action: JsonObject, data: JsonObject, version: Int?): JsonElement {
        val range = data["range"]?.jsonObject ?: throw IllegalArgumentException("no range on the action")
        val key = data["key"]?.jsonPrimitive?.contentOrNull ?: throw IllegalArgumentException("no key on the action")
        placeCaret(mirror, range)
        val changes = ReadAction.compute<Map<String, List<JsonObject>>, RuntimeException> {
            val offer = offersAt(mirror).firstOrNull { it.key == key } ?: throw StaleAction()
            val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document) ?: throw StaleAction()
            val command = offer.action.perform(ActionContext.from(mirror.editor, file))
            editsOf(command, mirror)
        }
        if (mirror.version != version && version != null) throw StaleAction()
        return buildJsonObject {
            put("title", action["title"] ?: JsonPrimitive("Quick fix"))
            put("kind", action["kind"] ?: JsonPrimitive(QUICK_FIX))
            put("edit", buildJsonObject {
                put("changes", buildJsonObject { changes.forEach { (uri, edits) -> put(uri, JsonArray(edits)) } })
            })
        }
    }

    /** The Mirror's caret and selection where the request was made: what the actions are computed for. */
    fun placeCaret(mirror: Mirror, range: JsonObject) {
        val start = MirrorSet.offset(mirror.document, range["start"]!!.jsonObject)
        val end = MirrorSet.offset(mirror.document, range["end"]!!.jsonObject)
        edt {
            mirror.editor.caretModel.moveToOffset(start)
            if (end > start) mirror.editor.selectionModel.setSelection(start, end) else mirror.editor.selectionModel.removeSelection()
        }
    }

    /** What a command changes, as edits by file. What needs a person, or touches more than text, is refused, naming what. */
    private fun editsOf(command: ModCommand, mirror: Mirror): Map<String, List<JsonObject>> {
        val out = LinkedHashMap<String, MutableList<JsonObject>>()
        for (step in command.unpack()) {
            when (step) {
                is ModUpdateFileText -> {
                    val path = java.nio.file.Path.of(step.file().path)
                    val uri = path.toUri().toString()
                    val current = if (uri == mirror.uri) mirror.document.text
                    else com.intellij.openapi.fileEditor.FileDocumentManager.getInstance().getDocument(step.file())?.text
                    if (current != null && current != step.oldText()) throw StaleAction()
                    out.getOrPut(uri) { ArrayList() } += TextEdits.diff(step.oldText(), step.newText())
                }
                // Where the caret goes, what to highlight, a message: nothing to change.
                is com.intellij.modcommand.ModNavigate, is com.intellij.modcommand.ModHighlight,
                is com.intellij.modcommand.ModNothing, is com.intellij.modcommand.ModDisplayMessage,
                is com.intellij.modcommand.ModCopyToClipboard, is com.intellij.modcommand.ModRegisterTabOut -> Unit
                is ModCompositeCommand -> Unit
                else -> throw Rename.Refused("this action needs more than a text edit (${step.javaClass.simpleName}), which the Bridge cannot do yet")
            }
        }
        return out
    }

    class StaleAction : RuntimeException("the buffer changed since the action was offered")

    private fun organizeImports(mirror: Mirror): List<JsonObject> {
        val doc = mirror.document
        val (original, file) = ReadAction.compute<Pair<String, PsiFile?>, RuntimeException> {
            doc.text to PsiDocumentManager.getInstance(project).getPsiFile(doc)
        }
        if (file == null) return emptyList()
        // As for formatting: the style (import layout, star-import thresholds) comes
        // from the ORIGINAL file, since a copy can lose its project context.
        val settings = ReadAction.compute<com.intellij.psi.codeStyle.CodeStyleSettings, RuntimeException> {
            CodeStyle.getSettings(file)
        }
        val optimizers = ReadAction.compute<List<com.intellij.lang.ImportOptimizer>, RuntimeException> {
            LanguageImportStatements.INSTANCE.forFile(file).filter { it.supports(file) }
        }
        val organized = edt {
            var result = original
            WriteCommandAction.runWriteCommandAction(project) {
                val copy = file.copy() as PsiFile
                CodeStyle.runWithLocalSettings(project, settings, Runnable {
                    // Chosen from the original: `supports` can say no to a copy, which has no
                    // virtual file, though the same optimiser handles it perfectly well.
                    for (optimizer in optimizers) optimizer.processFile(copy).run()
                })
                result = copy.text
            }
            result
        }
        return TextEdits.diff(original, organized)
    }
}
