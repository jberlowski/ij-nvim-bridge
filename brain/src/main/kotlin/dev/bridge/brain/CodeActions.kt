package dev.bridge.brain

import com.intellij.application.options.CodeStyle
import com.intellij.codeInsight.daemon.impl.ShowIntentionsPass
import com.intellij.modcommand.ActionContext
import com.intellij.modcommand.ModChooseAction
import com.intellij.modcommand.ModCommand
import com.intellij.modcommand.ModCommandAction
import com.intellij.modcommand.ModCompositeCommand
import com.intellij.modcommand.ModUpdateFileText
import com.intellij.lang.LanguageImportStatements
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiLocalVariable
import com.intellij.psi.util.PsiTreeUtil
import com.intellij.refactoring.inline.InlineLocalHandler
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
        /** How long listing may spend running offers to classify them (see [expandWithinBudget]). */
        const val EXPAND_BUDGET_MS = 100L
        const val ORGANIZE_IMPORTS = "source.organizeImports"
        const val GENERATE = "source.generate"
        const val QUICK_FIX = "quickfix"
        const val REWRITE = "refactor.rewrite"
        const val REFACTOR_INLINE = "refactor.inline"
        val KINDS = listOf(ORGANIZE_IMPORTS, GENERATE, QUICK_FIX, REWRITE, REFACTOR_INLINE)
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
        actions += inlineAt(mirror, file, params, only)
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
        val element = file.findElementAt(mirror.editor.caretModel.offset)
        for ((descriptors, kind) in groups) {
            for (descriptor in descriptors) {
                // The action itself, then what IntelliJ tucks under its arrow in the popup: "Suppress for
                // statement / method / class" and the like.
                val options: List<com.intellij.codeInsight.intention.IntentionAction> =
                    element?.let { runCatching { descriptor.getOptions(it, mirror.editor)?.toList() }.getOrNull() }.orEmpty()
                for (intention in listOf(descriptor.action) + options) {
                    val mod = intention.asModCommandAction() ?: continue
                    val presentation = mod.getPresentation(context) ?: continue
                    val title = presentation.name.ifBlank { intention.text }
                    val key = "${intention.familyName}|$title"
                    out.putIfAbsent(key, Offer(kind, key, title, mod))
                }
            }
        }
        return out.values.toList()
    }

    /** What one offer stands for, remembered: (kind, key, title) for each action it becomes, none if it changes no text. */
    private val expansions = java.util.concurrent.ConcurrentHashMap<String, List<Triple<String, String, String>>>()

    /**
     * [expand] for each offer, but for no longer than [EXPAND_BUDGET_MS] in all. Running an offer to see what it is costs
     * about 100 ms each (1.4 s the first time, measured), which is too long to make the menu wait for; so what is not
     * reached in time is listed as it is (resolving still says why an action cannot be applied), and what *was*
     * worked out is remembered for this file version and place, so asking again at the same spot finishes the job.
     */
    private fun expandWithinBudget(mirror: Mirror, offers: List<Offer>, context: ActionContext): List<Offer> {
        val deadline = System.nanoTime() + EXPAND_BUDGET_MS * 1_000_000
        val editor = mirror.editor
        val place = "${mirror.uri}|${mirror.version}|${editor.caretModel.offset}|${editor.selectionModel.selectionStart}-${editor.selectionModel.selectionEnd}|"
        if (expansions.size > 1024) expansions.clear()
        return offers.flatMap { offer ->
            val known = expansions[place + offer.key]
            when {
                known != null -> known.map { (kind, key, title) -> Offer(kind, key, title, offer.action) }
                System.nanoTime() > deadline -> listOf(offer)
                else -> expand(offer, context).also { result ->
                    expansions[place + offer.key] = result.map { Triple(it.kind, it.key, it.title) }
                }
            }
        }
    }

    /**
     * What one offer stands for in the list. Each offer is run for its `ModCommand` (as resolving does, and still
     * never executed), which answers two things a client cannot otherwise know:
     *  - an action that offers *choices* ([ModChooseAction]: "Change visibility...", "Convert number to...") is
     *    listed as one action per choice, since a code-action menu has no second step to choose in;
     *  - an action whose whole effect is to move the caret, highlight or show a message changes no text, so it is
     *    not listed ("Navigate to duplicate class" was offered, selectable, and did nothing).
     * An offer that cannot be run here is listed as it is: resolving says why it cannot be applied.
     */
    private fun expand(offer: Offer, context: ActionContext): List<Offer> {
        val command = runCatching { offer.action.perform(context) }.getOrNull() ?: return listOf(offer)
        val choose = chooseOf(command)
        if (choose == null) return if (changesNoText(command)) emptyList() else listOf(offer)
        val parent = offer.title.trimEnd('…', ' ', '.')
        return choose.actions().withIndex().mapNotNull { (i, choice) ->
            val name = choice.getPresentation(context)?.name?.takeIf { it.isNotBlank() } ?: return@mapNotNull null
            Offer(offer.kind, "${offer.key}#$i", "$parent: $name", choice)
        }
    }

    /** The choices a command offers, when offering them is all it does. */
    private fun chooseOf(command: ModCommand): ModChooseAction? = command.unpack().singleOrNull() as? ModChooseAction

    /** Every step only moves the caret, highlights or shows something: nothing a text-edit client can apply. */
    private fun changesNoText(command: ModCommand): Boolean = command.unpack().all(::isNoEditStep)

    private fun isNoEditStep(step: ModCommand): Boolean =
        step is com.intellij.modcommand.ModNavigate || step is com.intellij.modcommand.ModHighlight ||
            step is com.intellij.modcommand.ModNothing || step is com.intellij.modcommand.ModDisplayMessage ||
            step is com.intellij.modcommand.ModCopyToClipboard || step is com.intellij.modcommand.ModRegisterTabOut

    /**
     * "Inline variable" (FEATURES.md's extract/inline/move): Java only so far. `InlineLocalHandler`
     * is not an intention - it is a refactoring handler, invoked from its own shortcut or menu, not
     * `ShowIntentionsPass` - but reproducing against the real IDE found it is already `ModCommand`-based
     * (`InlineLocalHandler.doInline`), so it reuses [editsOf] exactly as an intention's `ModCommand`
     * does, needing no new edit-computation code. Kotlin's own inline (`KotlinInlinePropertyProcessor`)
     * is the older kind that mutates PSI directly when run, and a scratch copy of it produced no
     * change at all when tried - undocumented internal behaviour, not yet worth its own spike, so
     * Kotlin is a known gap (D2, §5): not offered rather than silently failing.
     */
    private fun inlineAt(mirror: Mirror, file: PsiFile, params: JsonObject, only: List<String>?): List<JsonObject> {
        if (!wanted(REFACTOR_INLINE, only)) return emptyList()
        val range = params["range"]?.jsonObject ?: return emptyList()
        val offset = MirrorSet.offset(mirror.document, range["start"]!!.jsonObject)
        val variable = inlinableVariableAt(file, offset) ?: return emptyList()
        return listOf(buildJsonObject {
            put("title", "Inline variable '${variable.name}'")
            put("kind", REFACTOR_INLINE)
            put("data", buildJsonObject {
                put("uri", mirror.uri)
                put("version", mirror.version)
                put("kind", REFACTOR_INLINE)
                put("range", range)
            })
        })
    }

    /** The local variable at the offset - its declaration, or a usage - if Java's own Inline can handle it. */
    private fun inlinableVariableAt(file: PsiFile, offset: Int): PsiLocalVariable? {
        val at = file.findElementAt(offset) ?: return null
        val variable = PsiTreeUtil.getParentOfType(at, PsiLocalVariable::class.java, false)
            ?: file.findReferenceAt(offset)?.resolve() as? PsiLocalVariable
            ?: return null
        return variable.takeIf { runCatching { InlineLocalHandler().canInlineElement(it) }.getOrDefault(false) }
    }

    /** The offer's own `ModCommand`, read via [editsOf] exactly as an intention's is - never executed. */
    private fun resolveInline(mirror: Mirror, action: JsonObject, data: JsonObject, version: Int?): JsonElement {
        val range = data["range"]?.jsonObject ?: throw IllegalArgumentException("no range on the action")
        val changes = ReadAction.compute<Map<String, List<JsonObject>>, RuntimeException> {
            val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document) ?: throw StaleAction()
            val offset = MirrorSet.offset(mirror.document, range["start"]!!.jsonObject)
            val variable = inlinableVariableAt(file, offset) ?: throw StaleAction()
            val context = ActionContext.from(mirror.editor, file)
            val command = InlineLocalHandler.doInline(context, variable, null, InlineLocalHandler.InlineMode.INLINE_ALL_AND_DELETE)
            editsOf(command, mirror)
        }
        if (mirror.version != version && version != null) throw StaleAction()
        return buildJsonObject {
            put("title", action["title"] ?: JsonPrimitive("Inline variable"))
            put("kind", action["kind"] ?: JsonPrimitive(REFACTOR_INLINE))
            put("edit", buildJsonObject {
                put("changes", buildJsonObject { changes.forEach { (uri, edits) -> put(uri, JsonArray(edits)) } })
            })
        }
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
        val context = ActionContext.from(mirror.editor, PsiDocumentManager.getInstance(project).getPsiFile(mirror.document) ?: return emptyList())
        val expanded = expandWithinBudget(mirror, offersAt(mirror).filter { wanted(it.kind, only) }, context)
        return expanded.map { offer ->
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

    /**
     * Not for a Kotlin script (`*.kts`, Gradle's build scripts among them): the optimiser needs the script's own
     * analysis context, which a copy of the file does not carry, and it fails inside K2 on the DSL's calls
     * (`KaBaseInvokeFunctionReference ... is missing in the map`). An action that can only fail is not offered.
     */
    private fun supportsImports(file: PsiFile) =
        !file.name.endsWith(".kts") && LanguageImportStatements.INSTANCE.forFile(file).any { it.supports(file) }

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
        if (kind == REFACTOR_INLINE) return resolveInline(mirror, action, data, version)
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
            // "key#2#0": the offer, then the third of its choices, then that one's first.
            val steps = key.split('#')
            val offer = offersAt(mirror).firstOrNull { it.key == steps[0] } ?: throw StaleAction()
            val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document) ?: throw StaleAction()
            val context = ActionContext.from(mirror.editor, file)
            var action: ModCommandAction = offer.action
            for (choice in steps.drop(1)) {
                val choose = chooseOf(action.perform(context)) ?: throw StaleAction()
                action = choose.actions().getOrNull(choice.toInt()) ?: throw StaleAction()
            }
            editsOf(action.perform(context), mirror)
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
            mirror.byBrain {
                mirror.editor.caretModel.moveToOffset(start)
                if (end > start) mirror.editor.selectionModel.setSelection(start, end) else mirror.editor.selectionModel.removeSelection()
            }
        }
    }

    /** What a command changes, as edits by file. What needs a person, or touches more than text, is refused, naming what. */
    private fun editsOf(command: ModCommand, mirror: Mirror): Map<String, List<JsonObject>> {
        val out = LinkedHashMap<String, MutableList<JsonObject>>()
        val steps = command.unpack()
        for (step in steps) {
            when (step) {
                is ModUpdateFileText -> {
                    val path = java.nio.file.Path.of(step.file().path)
                    val uri = path.toUri().toString()
                    val current = if (uri == mirror.uri) mirror.document.text
                    else com.intellij.openapi.fileEditor.FileDocumentManager.getInstance().getDocument(step.file())?.text
                    if (current != null && current != step.oldText()) throw StaleAction()
                    out.getOrPut(uri) { ArrayList() } += TextEdits.diff(step.oldText(), step.newText())
                }
                is ModCompositeCommand -> Unit
                // Where the caret goes, what to highlight, a message: nothing to change.
                else -> if (!isNoEditStep(step)) {
                    throw Rename.Refused("this action needs more than a text edit (${step.javaClass.simpleName}), which the Bridge cannot do yet")
                }
            }
        }
        // Reporting success with nothing to apply reads as broken: say so instead.
        if (out.isEmpty() && steps.isNotEmpty()) {
            throw Rename.Refused("this action changes no text: it only navigates or shows something")
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
