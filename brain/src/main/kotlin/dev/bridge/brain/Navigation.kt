package dev.bridge.brain

import com.intellij.codeInsight.TargetElementUtil
import com.intellij.codeInsight.documentation.DocumentationManager
import com.intellij.codeInsight.highlighting.ReadWriteAccessDetector
import com.intellij.codeInsight.navigation.actions.GotoDeclarationAction
import com.intellij.codeInsight.navigation.actions.GotoTypeDeclarationAction
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.progress.EmptyProgressIndicator
import com.intellij.openapi.progress.ProcessCanceledException
import com.intellij.openapi.project.IndexNotReadyException
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.psi.search.LocalSearchScope
import com.intellij.psi.search.searches.DefinitionsScopedSearch
import com.intellij.psi.search.searches.ReferencesSearch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The read-only navigation features of FEATURES.md §3-4, answered as ordinary LSP.
 *
 * Unlike completion, which must block the EDT, these run in cancellable
 * background read actions, so a long search never holds the UI thread. Every
 * answer is against the Mirror's text at the version the request arrived at:
 * if the Mirror changes before the answer is ready the reply is
 * `ContentModified`, never a stale result. The same reply is given while the
 * Brain is Indexing, since none of these can be answered without indices.
 */
class NavigationEngine(private val project: Project, private val brain: BrainService) : Disposable {

    /** One request in flight. Exactly one reply is ever sent, whoever gets there first. */
    private class Job(private val id: JsonElement?, private val transport: Transport) {
        val cancelled = AtomicBoolean(false)
        val indicator = EmptyProgressIndicator()
        private val replied = AtomicBoolean(false)

        fun reply(message: JsonObject) {
            if (replied.compareAndSet(false, true)) transport.send(message)
        }

        fun cancel() {
            cancelled.set(true)
            indicator.cancel()
            reply(Wire.error(id, RpcError.REQUEST_CANCELLED, "cancelled")) // at once, not when the worker wakes
        }
    }

    private val log = logger<NavigationEngine>()
    private val pool = Executors.newCachedThreadPool { r -> Thread(r, "bridge-navigation").apply { isDaemon = true } }
    private val inflight = ConcurrentHashMap<String, Job>()
    private val locations = Locations(project)
    private val structure = StructureFeatures(project, locations)
    private val signatures = SignatureHelp()
    private val formatting = Formatting(project)
    private val codeActions = CodeActions(project)
    private val insertion = CompletionInsertion(project, brain.completion.store)
    private val renames = Rename(project, locations)
    private val moves = FileMoves(project, locations)
    private val newFiles = NewFile(project)

    companion object {
        val METHODS = setOf(
            "textDocument/definition", "textDocument/typeDefinition", "textDocument/implementation",
            "textDocument/references", "textDocument/hover", "textDocument/documentHighlight",
            "textDocument/documentSymbol", "textDocument/foldingRange", "textDocument/selectionRange",
            "workspace/symbol", "textDocument/signatureHelp",
            "textDocument/formatting", "textDocument/rangeFormatting",
            "textDocument/codeAction", "codeAction/resolve", "completionItem/resolve",
            "textDocument/prepareRename", "textDocument/rename", "workspace/willRenameFiles", "\$/ij/newFile",
        )
        /** Edits: computed on a copy, needing a write action, so not read-only. */
        val FORMATTING = setOf("textDocument/formatting", "textDocument/rangeFormatting")
        /** Everything that computes an edit on a copy, and so needs the write path. */
        val EDITING = FORMATTING + "codeAction/resolve" + "completionItem/resolve" + "textDocument/rename"
        const val MAX_LOCATIONS = 5000
    }

    fun submit(method: String, id: JsonElement?, params: JsonObject, transport: Transport) {
        // codeAction/resolve carries the action, not a textDocument: its data names the file.
        val uri = params["textDocument"]?.jsonObject?.get("uri")?.jsonPrimitive?.contentOrNull
            ?: params["data"]?.jsonObject?.get("uri")?.jsonPrimitive?.contentOrNull
        val mirror = uri?.let { brain.mirrors.get(it) }
        // workspace/symbol asks about the project, not about a buffer.
        if (mirror == null && method != "workspace/symbol" && method != "workspace/willRenameFiles" && method != "\$/ij/newFile") {
            transport.send(Wire.error(id, RpcError.INVALID_PARAMS, "not mirrored: $uri"))
            return
        }
        val status = brain.status()
        // Formatting works on syntax, not indices, so it is allowed while Indexing.
        if (status.state != "Ready" && method !in FORMATTING) {
            transport.send(Wire.error(id, RpcError.CONTENT_MODIFIED,
                "IntelliJ is not ready (${status.reason}); ask again when it is"))
            return
        }
        val job = Job(id, transport)
        val key = id.toString()
        inflight[key] = job
        val versionAtStart = mirror?.version
        pool.execute {
            try {
                if (DebugLevers.navigationDelayMs > 0) Thread.sleep(DebugLevers.navigationDelayMs)
                // The buffer moved on while this waited its turn: the position asked about is stale.
                if (mirror != null && mirror.version != versionAtStart) throw CodeActions.StaleAction()
                if (method == "textDocument/signatureHelp") {
                    // Some parameter-info handlers read the caret, not the offset they are given.
                    val at = MirrorSet.offset(mirror!!.document, params["position"]!!.jsonObject)
                    edt { mirror.editor.caretModel.moveToOffset(at) }
                }
                val result = if (method in EDITING) {
                    // A write action on the EDT: never from inside a read action.
                    if (method == "codeAction/resolve") codeActions.resolve(mirror!!, params)
                    else if (method == "completionItem/resolve") insertion.resolve(mirror!!, params)
                    else if (method == "textDocument/rename") renames.rename(mirror!!, params)
                    else formatting.edits(mirror!!, params["range"] as? JsonObject)
                } else {
                    ReadAction.nonBlocking(Callable { compute(method, mirror, params) })
                        .wrapProgress(job.indicator)
                        .executeSynchronously()
                }
                when {
                    job.cancelled.get() -> job.cancel()
                    mirror != null && mirror.version != versionAtStart ->
                        job.reply(Wire.error(id, RpcError.CONTENT_MODIFIED, "the buffer changed while answering"))
                    else -> job.reply(Wire.response(id, result))
                }
            } catch (_: ProcessCanceledException) {
                if (job.cancelled.get()) job.cancel()
                else job.reply(Wire.error(id, RpcError.CONTENT_MODIFIED, "content modified"))
            } catch (e: Rename.Refused) {
                job.reply(Wire.error(id, RpcError.INVALID_PARAMS, e.message ?: "cannot rename"))
            } catch (_: CodeActions.StaleAction) {
                job.reply(Wire.error(id, RpcError.CONTENT_MODIFIED, "the buffer changed since the action was offered"))
            } catch (_: IndexNotReadyException) {
                job.reply(Wire.error(id, RpcError.CONTENT_MODIFIED, "IntelliJ is indexing; ask again when it is done"))
            } catch (t: Throwable) {
                log.warn("bridge: $method failed", t)
                brain.record.error(transport.session, method, t)
                job.reply(Wire.error(id, RpcError.INTERNAL, "${t::class.java.simpleName}: ${t.message}"))
            } finally {
                inflight.remove(key)
            }
        }
    }

    /** `$/cancelRequest`. */
    fun cancel(id: JsonElement?) {
        inflight[id.toString()]?.cancel()
    }

    // ---------------------------------------------------------------- features
    private fun compute(method: String, mirror: Mirror?, params: JsonObject): JsonElement {
        if (method == "workspace/symbol") {
            return structure.workspaceSymbols(params["query"]?.jsonPrimitive?.contentOrNull ?: "")
        }
        if (method == "workspace/willRenameFiles") return moves.willRename(params)
        if (method == "\$/ij/newFile") return newFiles.create(params)
        mirror!!
        val doc = mirror.document
        // selectionRange carries a list of positions instead of one.
        val offset = params["position"]?.jsonObject?.let { MirrorSet.offset(doc, it) } ?: 0
        val editor = mirror.editor
        val file = PsiDocumentManager.getInstance(project).getPsiFile(doc)
            ?: return JsonNull
        return when (method) {
            "textDocument/definition" -> locationsOf(
                GotoDeclarationAction.findAllTargetElements(project, editor, offset)?.filterNotNull().orEmpty())
            "textDocument/typeDefinition" -> locationsOf(
                GotoTypeDeclarationAction.findSymbolTypes(editor, offset)?.filterNotNull().orEmpty())
            "textDocument/implementation" -> {
                val target = target(editor, offset) ?: return JsonArray(emptyList())
                locationsOf(DefinitionsScopedSearch.search(target, GlobalSearchScope.projectScope(project))
                    .findAll().toList())
            }
            "textDocument/references" -> references(editor, offset, params)
            "textDocument/hover" -> hover(editor, file, offset)
            "textDocument/documentHighlight" -> highlights(editor, file, offset)
            "textDocument/signatureHelp" -> signatures.signatureHelp(project, editor, file, offset)
            "textDocument/codeAction" -> codeActions.list(mirror, params)
            "textDocument/prepareRename" -> renames.prepare(editor, file, offset)
            "textDocument/documentSymbol" -> structure.documentSymbols(editor, file)
            "textDocument/foldingRange" -> structure.foldingRanges(file, doc)
            "textDocument/selectionRange" -> structure.selectionRanges(
                editor, file, (params["positions"] as? JsonArray)?.map { it.jsonObject } ?: emptyList())
            else -> throw IllegalArgumentException("not a navigation method: $method")
        }
    }

    private fun target(editor: com.intellij.openapi.editor.Editor, offset: Int): PsiElement? =
        TargetElementUtil.getInstance().findTargetElement(editor, TargetElementUtil.getInstance().allAccepted, offset)

    private fun locationsOf(elements: List<PsiElement>): JsonArray =
        JsonArray(elements.distinct().mapNotNull { locations.of(it) })

    private fun references(editor: com.intellij.openapi.editor.Editor, offset: Int, params: JsonObject): JsonElement {
        val target = target(editor, offset) ?: return JsonArray(emptyList())
        val includeDeclaration = params["context"]?.jsonObject?.get("includeDeclaration")
            ?.jsonPrimitive?.contentOrNull == "true"
        val found = ArrayList<JsonObject>()
        if (includeDeclaration) locations.of(target)?.let { found += it }
        for (ref in ReferencesSearch.search(target, GlobalSearchScope.projectScope(project)).findAll()) {
            val element = ref.element
            val file = element.containingFile ?: continue
            val start = element.textRange.startOffset + ref.rangeInElement.startOffset
            val range = com.intellij.openapi.util.TextRange(start, start + ref.rangeInElement.length)
            locations.locate(file, range)?.let { found += it }
            if (found.size >= MAX_LOCATIONS) break
        }
        return JsonArray(found.distinctBy { it.toString() })
    }

    private fun highlights(editor: com.intellij.openapi.editor.Editor, file: PsiFile, offset: Int): JsonElement {
        val target = target(editor, offset) ?: return JsonArray(emptyList())
        val detector = ReadWriteAccessDetector.findDetector(target)
        val doc = editor.document
        val out = ArrayList<JsonObject>()
        fun add(range: com.intellij.openapi.util.TextRange, kind: Int) {
            out += buildJsonObject {
                put("range", buildJsonObject {
                    put("start", Locations.position(doc, range.startOffset))
                    put("end", Locations.position(doc, range.endOffset))
                })
                put("kind", kind)
            }
        }
        // The declaration, when it is in this file.
        if (target.containingFile?.originalFile == file.originalFile) {
            (target as? com.intellij.psi.PsiNameIdentifierOwner)?.nameIdentifier?.textRange?.let { add(it, 1) }
        }
        for (ref in ReferencesSearch.search(target, LocalSearchScope(file)).findAll()) {
            val start = ref.element.textRange.startOffset + ref.rangeInElement.startOffset
            val access = detector?.getReferenceAccess(target, ref)
            // LSP: 1 Text, 2 Read, 3 Write.
            val kind = when (access) {
                ReadWriteAccessDetector.Access.Write, ReadWriteAccessDetector.Access.ReadWrite -> 3
                ReadWriteAccessDetector.Access.Read -> 2
                else -> 1
            }
            add(com.intellij.openapi.util.TextRange(start, start + ref.rangeInElement.length), kind)
        }
        return JsonArray(out.distinctBy { it.toString() })
    }

    /**
     * Quick documentation. Kotlin (K2) documents symbols through IntelliJ's newer
     * documentation-target API and the older PSI provider returns nothing for it,
     * while Java answers to both; so ask the target API first and the provider
     * second.
     */
    private fun hover(editor: com.intellij.openapi.editor.Editor, file: PsiFile, offset: Int): JsonElement {
        val context = file.findElementAt(offset) ?: return JsonNull
        var signature: String? = null
        var documentation: String? = null

        // Some targets (Java's) need a coroutine context this thread does not have
        // and throw IllegalStateException; the older provider answers for those.
        // The two halves fall back independently: a target can supply the
        // signature and still throw when asked for the documentation.
        val targets = runCatching {
            com.intellij.lang.documentation.ide.IdeDocumentationTargetProvider.getInstance(project)
                .documentationTargets(editor, file, offset)
        }.getOrDefault(emptyList())
        for (target in targets) {
            signature = signature ?: runCatching { target.computeDocumentationHint() }.getOrNull()
            documentation = documentation ?: runCatching {
                (target.computeDocumentation() as? com.intellij.platform.backend.documentation.DocumentationData)?.html
            }.getOrNull()
        }
        if (signature == null || documentation == null) {
            val element = DocumentationManager.getInstance(project).findTargetElement(editor, offset, file, context)
            if (element != null) {
                val provider = DocumentationManager.getProviderFromElement(element)
                signature = signature ?: provider.getQuickNavigateInfo(element, context)
                documentation = documentation ?: provider.generateDoc(element, context)
            }
        }

        val markdown = listOfNotNull(
            signature?.let { HtmlToMarkdown.convert(it) }?.takeIf { it.isNotBlank() }
                ?.let { if (it.startsWith("```")) it else "```\n$it\n```" },
            documentation?.let { HtmlToMarkdown.convert(it) }?.takeIf { it.isNotBlank() },
        ).distinct().joinToString("\n\n---\n\n")
        if (markdown.isBlank()) return JsonNull
        val doc = editor.document
        return buildJsonObject {
            put("contents", buildJsonObject { put("kind", "markdown"); put("value", markdown) })
            put("range", buildJsonObject {
                put("start", Locations.position(doc, context.textRange.startOffset))
                put("end", Locations.position(doc, context.textRange.endOffset))
            })
        }
    }

    override fun dispose() {
        pool.shutdownNow()
    }
}

/** IntelliJ's documentation is HTML; LSP clients want Markdown. Deliberately small. */
object HtmlToMarkdown {
    fun convert(html: String): String {
        var s = html
        s = s.replace(Regex("(?is)<style.*?</style>"), "")
        s = s.replace(Regex("(?is)<pre[^>]*>(.*?)</pre>")) { "\n```\n" + strip(it.groupValues[1]) + "\n```\n" }
        s = s.replace(Regex("(?is)<code[^>]*>(.*?)</code>")) { "`" + strip(it.groupValues[1]) + "`" }
        s = s.replace(Regex("(?is)<(b|strong)[^>]*>(.*?)</\\1>")) { "**" + it.groupValues[2] + "**" }
        s = s.replace(Regex("(?is)<(i|em)[^>]*>(.*?)</\\1>")) { "*" + it.groupValues[2] + "*" }
        s = s.replace(Regex("(?i)<br\\s*/?>"), "\n")
        s = s.replace(Regex("(?i)</(p|div|tr|li|h[1-6]|table|dl|dd|dt)>"), "\n")
        s = s.replace(Regex("(?i)<li[^>]*>"), "- ")
        s = strip(s)
        return s.lines().joinToString("\n") { it.trimEnd() }.replace(Regex("\n{3,}"), "\n\n").trim()
    }

    private fun strip(s: String): String =
        s.replace(Regex("(?s)<[^>]+>"), "")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
            .replace("&#39;", "'").replace("&nbsp;", " ").replace("&amp;", "&")
}
