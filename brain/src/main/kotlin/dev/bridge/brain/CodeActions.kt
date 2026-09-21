package dev.bridge.brain

import com.intellij.application.options.CodeStyle
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
        val KINDS = listOf(ORGANIZE_IMPORTS)
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
        return JsonArray(actions)
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
        val edits = when (data["kind"]?.jsonPrimitive?.contentOrNull) {
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
