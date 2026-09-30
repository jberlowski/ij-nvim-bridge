package dev.bridge.brain

import com.intellij.codeInsight.daemon.DaemonCodeAnalyzer
import com.intellij.codeInsight.daemon.impl.HighlightInfo
import com.intellij.lang.annotation.HighlightSeverity
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.colors.CodeInsightColors
import com.intellij.openapi.editor.impl.DocumentMarkupModel
import com.intellij.openapi.fileEditor.FileEditor
import com.intellij.openapi.project.Project
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Diagnostics harvested from the daemon's markup model (SPEC.md §9).
 *
 * *Cannot resolve symbol*, syntax errors and type errors come from annotators
 * and highlight visitors, not from InspectionEngine, so the daemon is the
 * source. It only analyses editors IntelliJ believes are in use, which is why
 * Mirrors are real editors and why the Editor tells the Brain which buffer is
 * active ($/ij/focus).
 *
 * Trigger: the daemon finishing a pass. It delivers progressively and can
 * finish a pass that was then cancelled, so publishes are coalesced (~150 ms)
 * and identical results are not re-sent. Each publish is a full-file
 * replacement. While the Brain is Indexing nothing is published: a stale or
 * "not resolved until the project is fully loaded" answer is worse than none.
 */
class DiagnosticsPublisher(private val project: Project, private val brain: BrainService) : Disposable {

    private val log = logger<DiagnosticsPublisher>()
    private val scheduler = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "bridge-diagnostics").apply { isDaemon = true }
    }
    private val pending = ConcurrentHashMap<String, ScheduledFuture<*>>()
    private val lastPublished = ConcurrentHashMap<String, JsonArray>()

    init {
        project.messageBus.connect(this).subscribe(
            DaemonCodeAnalyzer.DAEMON_EVENT_TOPIC,
            object : DaemonCodeAnalyzer.DaemonListener {
                override fun daemonFinished(fileEditors: Collection<FileEditor>) {
                    for (editor in fileEditors) {
                        val file = editor.file ?: continue
                        brain.mirrors.byFile(file)?.let { schedule(it.uri) }
                    }
                }
            },
        )
    }

    fun schedule(uri: String) {
        pending.compute(uri) { _, old ->
            old?.cancel(false)
            scheduler.schedule({ publish(uri) }, COALESCE_MS, TimeUnit.MILLISECONDS)
        }
    }

    /** Leaving Indexing: what was withheld is now due, for every Mirror. */
    fun republishAll() {
        lastPublished.clear()
        DaemonCodeAnalyzer.getInstance(project).restart()
        brain.mirrors.all().forEach { schedule(it.uri) }
    }

    /** The Mirror is gone; so are its diagnostics. */
    fun clear(uri: String) {
        pending.remove(uri)?.cancel(false)
        lastPublished.remove(uri)
        send(uri, null, JsonArray(emptyList()))
    }

    private fun publish(uri: String) {
        try {
            val mirror = brain.mirrors.get(uri) ?: return
            if (brain.state() != "Ready") return // withheld, not stale
            val diagnostics = ApplicationManager.getApplication().runReadAction<JsonArray> {
                collect(mirror.document)
            }
            if (lastPublished.put(uri, diagnostics) == diagnostics) return
            send(uri, mirror.version, diagnostics)
        } catch (t: Throwable) {
            log.warn("bridge: publishing diagnostics for $uri failed", t)
        }
    }

    private fun send(uri: String, version: Int?, diagnostics: JsonArray) {
        brain.broadcast(Wire.notification("textDocument/publishDiagnostics", buildJsonObject {
            put("uri", uri)
            if (version != null) put("version", version)
            put("diagnostics", diagnostics)
        }))
    }

    private fun collect(doc: Document): JsonArray {
        val markup = DocumentMarkupModel.forDocument(doc, project, false) ?: return JsonArray(emptyList())
        val seen = HashSet<Triple<Int, Int, String>>()
        val items = markup.allHighlighters
            .mapNotNull { HighlightInfo.fromRangeHighlighter(it) }
            .filter { !it.description.isNullOrBlank() } // semantic-highlight spans have none
            .sortedWith(compareBy({ it.startOffset }, { it.endOffset }))
            .filter { seen.add(Triple(it.startOffset, it.endOffset, it.description)) }
            .map { toDiagnostic(doc, it) }
        return JsonArray(items)
    }

    private fun toDiagnostic(doc: Document, info: HighlightInfo): JsonObject {
        val length = doc.textLength
        val start = info.startOffset.coerceIn(0, length)
        var end = info.endOffset.coerceIn(start, length)
        if (end == start && end < length) end += 1 // an empty range is invisible in most clients
        return buildJsonObject {
            put("range", buildJsonObject {
                put("start", position(doc, start))
                put("end", position(doc, end))
            })
            put("severity", severity(info.severity))
            // LSP has four severities; IntelliJ has more (INFORMATION 10, TEXT ATTRIBUTES 11, SERVER PROBLEM 100,
            // WEAK WARNING 200, WARNING 300, ERROR 400, and any the IDE registers). Everything below WEAK WARNING is
            // "hint" above, so the IDE's own is sent as well, for a client that wants to tell them apart.
            put("data", buildJsonObject {
                put("ijSeverity", info.severity.name)
                put("ijSeverityValue", info.severity.myVal)
            })
            put("message", info.description)
            put("source", "IntelliJ")
            // Errors from annotators carry no inspection id; inspections do.
            info.inspectionToolId?.takeIf { it.isNotBlank() }?.let { put("code", it) }
            // LSP DiagnosticTag.Unnecessary (1): what IntelliJ shows greyed out (unused imports,
            // unused locals, unused declarations), so clients grey it out too, as IntelliJ does.
            if (isUnnecessary(info)) put("tags", JsonArray(listOf(kotlinx.serialization.json.JsonPrimitive(1))))
        }
    }

    /**
     * Whether IntelliJ greys this out: [HighlightInfo.getSeverity]/[ProblemHighlightType] do not tell WEAK_WARNING
     * apart from the greyed-out kind (an unused-import warning and an ordinary weak-warning squiggle are both just
     * WEAK_WARNING/WARNING), so this reads the actual editor text-attributes key instead - `forcedTextAttributesKey`
     * if the highlight sets one, else the key its own [HighlightInfoType] carries - and checks it against IntelliJ's
     * own key for "unused" (`NOT_USED_ELEMENT_ATTRIBUTES`), the same key an unused import, unused local, unused
     * method or unused class all render with. Confirmed against real highlights for both languages.
     */
    private fun isUnnecessary(info: HighlightInfo): Boolean =
        (info.forcedTextAttributesKey ?: info.type.attributesKey) == CodeInsightColors.NOT_USED_ELEMENT_ATTRIBUTES

    private fun position(doc: Document, offset: Int): JsonObject {
        if (doc.textLength == 0) return buildJsonObject { put("line", 0); put("character", 0) }
        val line = doc.getLineNumber(offset)
        return buildJsonObject {
            put("line", line)
            put("character", offset - doc.getLineStartOffset(line)) // UTF-16, as Document counts
        }
    }

    /** IntelliJ HighlightSeverity -> LSP DiagnosticSeverity (1 error .. 4 hint). */
    private fun severity(s: HighlightSeverity): Int = when {
        s >= HighlightSeverity.ERROR -> 1
        s >= HighlightSeverity.WARNING -> 2
        s >= HighlightSeverity.WEAK_WARNING -> 3
        else -> 4
    }

    override fun dispose() {
        scheduler.shutdownNow()
    }

    companion object {
        const val COALESCE_MS = 150L
    }
}
