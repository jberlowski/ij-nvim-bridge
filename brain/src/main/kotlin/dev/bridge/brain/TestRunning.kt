package dev.bridge.brain

import com.intellij.codeInsight.daemon.DaemonCodeAnalyzer
import com.intellij.execution.actions.ConfigurationContext
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.execution.testframework.sm.runner.SMTRunnerEventsAdapter
import com.intellij.execution.testframework.sm.runner.SMTRunnerEventsListener
import com.intellij.execution.testframework.sm.runner.SMTestProxy
import com.intellij.execution.testframework.sm.runner.events.TestOutputEvent
import com.intellij.execution.testframework.sm.runner.states.TestStateInfo
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.impl.DocumentMarkupModel
import com.intellij.openapi.externalSystem.model.ProjectSystemId
import com.intellij.openapi.externalSystem.model.execution.ExternalSystemTaskExecutionSettings
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskId
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskNotificationListener
import com.intellij.openapi.externalSystem.service.execution.ExternalSystemRunConfiguration
import com.intellij.openapi.externalSystem.service.execution.ProgressExecutionMode
import com.intellij.openapi.externalSystem.util.ExternalSystemUtil
import com.intellij.openapi.externalSystem.util.task.TaskExecutionSpec
import com.intellij.openapi.fileEditor.FileEditor
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.testIntegration.TestFinderHelper
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * "What can be run, shown in the sign column" (FEATURES.md §6c): `$/ij/runnables`, harvested from
 * the same markup the daemon already put on the Mirror's editor - IntelliJ's own
 * `RunLineMarkerProvider` gutter icons, JUnit 4/5, Kotlin tests and `main` alike, found and
 * confirmed by reproducing against the real IDE before writing this (a run marker's
 * `gutterIconRenderer` is `RunLineMarkerProvider$RunLineMarkerInfo`, the same way an unused-import
 * diagnostic's greyed-out colour is found in [DiagnosticsPublisher]). Coalesced and published per
 * file the same way diagnostics are; nothing runs to compute this, only what the daemon already did.
 */
class RunnablesPublisher(private val project: Project, private val brain: BrainService) : Disposable {

    private val log = logger<RunnablesPublisher>()
    private val scheduler = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "bridge-runnables").apply { isDaemon = true }
    }
    private val pending = ConcurrentHashMap<String, ScheduledFuture<*>>()

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
            scheduler.schedule({ publish(uri) }, DiagnosticsPublisher.COALESCE_MS, TimeUnit.MILLISECONDS)
        }
    }

    fun clear(uri: String) {
        pending.remove(uri)?.cancel(false)
        send(uri, JsonArray(emptyList()))
    }

    private fun publish(uri: String) {
        try {
            val mirror = brain.mirrors.get(uri) ?: return
            if (brain.state() != "Ready") return
            val runnables = ApplicationManager.getApplication().runReadAction<JsonArray> { collect(mirror) }
            send(uri, runnables)
        } catch (t: Throwable) {
            log.warn("bridge: publishing runnables for $uri failed", t)
        }
    }

    private fun send(uri: String, runnables: JsonArray) {
        brain.broadcast(Wire.notification("\$/ij/runnables", buildJsonObject {
            put("uri", uri)
            put("runnables", runnables)
        }))
    }

    private fun collect(mirror: Mirror): JsonArray {
        val doc = mirror.document
        val markup = DocumentMarkupModel.forDocument(doc, project, false) ?: return JsonArray(emptyList())
        val file = PsiDocumentManager.getInstance(project).getPsiFile(doc)
        val items = markup.allHighlighters
            .filter { it.gutterIconRenderer?.let { r -> RUN_MARKER in r::class.java.name } == true }
            .sortedBy { it.startOffset }
            .map { h ->
                val name = doc.getText(com.intellij.openapi.util.TextRange(h.startOffset, h.endOffset))
                val kind = file?.let { kindOf(it, h.startOffset, name) } ?: "method"
                buildJsonObject {
                    put("range", buildJsonObject {
                        put("start", Locations.position(doc, h.startOffset))
                        put("end", Locations.position(doc, h.endOffset))
                    })
                    put("name", name)
                    put("kind", kind)
                    put("title", stripHtml(h.gutterIconRenderer?.tooltipText.orEmpty()))
                }
            }
        return JsonArray(items)
    }

    /** "class" if this marker sits on the enclosing test class's own name, else "method". */
    private fun kindOf(file: com.intellij.psi.PsiFile, offset: Int, name: String): String {
        val at = file.findElementAt(offset) ?: return "method"
        val source = TestFinderHelper.findSourceElement(at) as? com.intellij.psi.PsiNameIdentifierOwner ?: return "method"
        return if (source.name == name) "class" else "method"
    }

    private fun stripHtml(html: String): String =
        html.replace(Regex("<[^>]+>"), "").replace("&nbsp;", " ").replace(Regex(" {2,}"), " ").trim()

    override fun dispose() {
        scheduler.shutdownNow()
    }

    companion object {
        /** `RunLineMarkerProvider$RunLineMarkerInfo` - confirmed by reproducing against the real IDE. */
        const val RUN_MARKER = "RunLineMarkerProvider"
    }
}

/**
 * "Run the test" (FEATURES.md §6c): `\$/ij/run`. The Run action of the marker at a position is
 * performed exactly as IntelliJ performs it - `ConfigurationContext` at that PSI element, the same
 * API right-clicking "Run" or clicking the gutter icon resolves through - so which test framework,
 * and whether Gradle or IntelliJ's own JUnit runner executes it, are the project's own settings
 * (Borrowed Settings), never decided here.
 *
 * v1 only follows through when that resolves to a Gradle-backed configuration (confirmed to be this
 * project's own setting, and the common case for a Gradle project); a native JUnit-runner
 * configuration is refused with a clear reason rather than silently doing nothing. Reproducing this
 * against the real IDE first found that the generic `ProgramRunnerUtil.executeConfiguration` path
 * produces no output and exits immediately for a `GradleRunConfiguration` in this headless harness
 * (cause not established), where driving the same settings through `ExternalSystemUtil.runTask` -
 * exactly as [GradleTasks] already does, tested - works and streams real output. So a resolved
 * Gradle configuration's own task names and script parameters are extracted and handed to that same
 * machinery, kept separate from `GradleTasks`' own tracking so a running "Gradle task" and a running
 * "test" never contend over one shared slot (the cost: the two could, in principle, run at once).
 *
 * Test-level results come from the same `SMTRunnerEventsListener` the Test Results tool window uses,
 * confirmed live: a run genuinely produces `PASSED_INDEX`/`FAILED_INDEX` events, not just a process
 * exit code. Reported per test as `\$/ij/test/status`; console output streams as `\$/ij/run/output`;
 * the whole run ends with `\$/ij/run/finished`. `scope` chooses the anchor PSI element - "nearest"
 * (whatever the position is in - a method, or a class if not inside one, exactly as a real click at
 * that position would) or "class"/"file" (the enclosing test class, found the same language-agnostic
 * way as go-to-test's `TestFinderHelper.findSourceElement`; "file" does not yet distinguish several
 * top-level classes in one file from the nearest one - a known v1 gap).
 */
class TestRunner(private val project: Project, private val brain: BrainService) {

    private class Run(val id: String, val transport: Transport) {
        val started = System.nanoTime()
        @Volatile var taskId: ExternalSystemTaskId? = null
        @Volatile var cancelled = false
        @Volatile var failure: String? = null
        @Volatile var succeeded = false
        val finished = AtomicBoolean(false)
        val output = OutputBatcher { text, stdout ->
            transport.send(Wire.notification("\$/ij/run/output", buildJsonObject {
                put("runId", id); put("text", text); put("stdout", stdout)
            }))
        }
    }

    private val current = AtomicReference<Run?>()

    fun run(mirror: Mirror, params: JsonObject, transport: Transport): JsonElement {
        val position = params["position"]?.jsonObject ?: throw Rename.Refused("no position given")
        val offset = MirrorSet.offset(mirror.document, position)
        val scope = params["scope"]?.jsonPrimitive?.contentOrNull ?: "nearest"
        val run = Run(UUID.randomUUID().toString().take(8), transport)
        if (!current.compareAndSet(null, run)) {
            throw Rename.Refused("a test is already running: stop it first")
        }
        ApplicationManager.getApplication().invokeLater {
            try {
                start(mirror, offset, scope, run)
            } catch (t: Throwable) {
                run.failure = "${t::class.java.simpleName}: ${t.message}"
                brain.record.error(transport.session, "test run", t)
                finish(run)
            }
        }
        return buildJsonObject { put("runId", run.id) }
    }

    /** Must run on the EDT: `ConfigurationContext`'s producers assume it (confirmed by reproducing). */
    private fun start(mirror: Mirror, offset: Int, scope: String, run: Run) {
        val anchor = ReadAction.compute<PsiElement?, RuntimeException> { anchorElement(mirror, offset, scope) }
        if (anchor == null) {
            run.failure = "nothing runnable at that position"; finish(run); return
        }
        val configFromContext = ReadAction.compute<com.intellij.execution.actions.ConfigurationFromContext?, RuntimeException> {
            ConfigurationContext(anchor).configurationsFromContext?.firstOrNull()
        }
        if (configFromContext == null) {
            run.failure = "no run configuration found for this position"; finish(run); return
        }
        val settings = configFromContext.configurationSettings
        val es = settings.configuration as? ExternalSystemRunConfiguration
        if (es == null) {
            run.failure = "not a Gradle-run test (${settings.configuration.type.displayName}); only Gradle-run tests are supported so far"
            finish(run); return
        }
        val connection = project.messageBus.connect()
        connection.subscribe(SMTRunnerEventsListener.TEST_STATUS, object : SMTRunnerEventsAdapter() {
            override fun onTestStarted(test: SMTestProxy) {
                if (!test.isSuite) sendStatus(run, test, "started")
            }
            override fun onTestFinished(test: SMTestProxy) {
                if (!test.isSuite) sendStatus(run, test, statusOf(test.magnitudeInfo))
            }
            override fun onSuiteStarted(suite: SMTestProxy) = sendSuite(run, suite, "started")
            override fun onSuiteFinished(suite: SMTestProxy) = sendSuite(run, suite, statusOf(suite.magnitudeInfo))
            // What the test itself printed, as it comes: the Gradle log keeps almost none of it.
            override fun onTestOutput(test: SMTestProxy, event: TestOutputEvent) {
                run.transport.send(Wire.notification("\$/ij/test/output", buildJsonObject {
                    put("runId", run.id)
                    put("path", pathOf(test))
                    put("text", event.text)
                    put("stdout", event.outputType.toString() != "stderr")
                }))
            }
        })
        val gsettings = ExternalSystemTaskExecutionSettings().apply {
            externalProjectPath = es.settings.externalProjectPath
            taskNames = rerunning(es.settings.taskNames)
            scriptParameters = listOfNotNull(es.settings.scriptParameters?.takeIf { it.isNotBlank() }, "--no-build-cache").joinToString(" ")
            externalSystemIdString = "GRADLE"
        }
        val listener = object : ExternalSystemTaskNotificationListener {
            override fun onStart(id: ExternalSystemTaskId) { run.taskId = id }
            override fun onStart(id: ExternalSystemTaskId, workingDir: String?) { run.taskId = id }
            override fun onTaskOutput(id: ExternalSystemTaskId, text: String, stdOut: Boolean) = run.output.add(text, stdOut)
            override fun onSuccess(id: ExternalSystemTaskId) { run.succeeded = true }
            override fun onFailure(id: ExternalSystemTaskId, e: Exception) { run.failure = e.message ?: e::class.java.simpleName }
            override fun onCancel(id: ExternalSystemTaskId) { run.cancelled = true }
            // The process ending and the SM framework finishing parsing its last test-result output
            // are two different pipelines: onEnd can fire a moment before the last test's own
            // onTestFinished does (confirmed - a full-suite run caught a $/ij/run/finished that beat
            // its own last $/ij/test/status once). A short settle keeps the order a client expects:
            // every test's result, then the summary.
            override fun onEnd(id: ExternalSystemTaskId) {
                java.util.concurrent.CompletableFuture.delayedExecutor(500, java.util.concurrent.TimeUnit.MILLISECONDS).execute {
                    connection.disconnect()
                    finish(run)
                }
            }
        }
        val spec = TaskExecutionSpec.create(project, ProjectSystemId("GRADLE"), DefaultRunExecutor.EXECUTOR_ID, gsettings)
            .withProgressExecutionMode(ProgressExecutionMode.IN_BACKGROUND_ASYNC)
            .withListener(listener)
            .build()
        brain.record.info(run.transport.session, "test_run") {
            put("runId", run.id); put("scope", scope); put("taskNames", gsettings.taskNames.joinToString(" "))
        }
        ExternalSystemUtil.runTask(spec)
    }

    /**
     * Gradle skips a test task whose inputs have not changed ("UP-TO-DATE"), so a test run again unchanged would
     * report success without running anything; a build cache hit ("FROM-CACHE") does the same. IntelliJ's own Run
     * always runs the tests: each test task gets its `clean` twin first (`:sub:test` -> `:sub:cleanTest`), and the
     * build cache is off for the run (`--no-build-cache`, which every Gradle version has).
     */
    private fun rerunning(tasks: List<String>): List<String> {
        val cleans = tasks.filter { !it.startsWith("-") && it.substringAfterLast(':').endsWith("test", ignoreCase = true) }
            .map { task ->
                val name = task.substringAfterLast(':')
                task.substring(0, task.length - name.length) + "clean" + name.replaceFirstChar { it.uppercase() }
            }
        return (cleans + tasks).distinct()
    }

    private fun sendStatus(run: Run, test: SMTestProxy, status: String) {
        run.transport.send(Wire.notification("\$/ij/test/status", buildJsonObject {
            put("runId", run.id)
            put("name", test.name)
            put("path", pathOf(test))
            put("status", status)
            test.duration?.let { put("ms", it) }
            // Gradle's results carry no separate message: it is the stack trace's first line.
            val trace = test.stacktrace?.takeIf { it.isNotBlank() }
            (test.errorMessage?.takeIf { it.isNotBlank() } ?: trace?.lineSequence()?.firstOrNull())?.let { put("message", it) }
            trace?.let { put("stacktrace", it) }
            // A failed assertion's two sides, as IntelliJ's own "Click to see difference" has them.
            test.diffViewerProvider?.let { diff ->
                put("expected", diff.left)
                put("actual", diff.right)
            }
        }))
    }

    /** A class (or other suite) of tests: the tree's interior. */
    private fun sendSuite(run: Run, suite: SMTestProxy, status: String) {
        val path = pathOf(suite)
        if (path.isEmpty()) return // the run's own root
        run.transport.send(Wire.notification("\$/ij/test/suite", buildJsonObject {
            put("runId", run.id)
            put("path", path)
            put("status", status)
            suite.duration?.let { put("ms", it) }
        }))
    }

    /**
     * The names from the top of the tree down to [test]: which class a test is in. Left out: the run's own
     * root and the two wrappers Gradle puts above every class ("Gradle Test Run", "Gradle Test Executor"),
     * which say nothing a developer asked about.
     */
    private fun pathOf(test: SMTestProxy): JsonArray {
        val names = ArrayList<String>()
        var at: SMTestProxy? = test
        while (at != null && at.parent != null) {
            if (!at.name.startsWith("Gradle Test Run") && !at.name.startsWith("Gradle Test Executor")) names += at.name
            at = at.parent as? SMTestProxy
        }
        return JsonArray(names.reversed().map(::JsonPrimitive))
    }

    private fun statusOf(magnitude: TestStateInfo.Magnitude): String = when (magnitude) {
        TestStateInfo.Magnitude.PASSED_INDEX, TestStateInfo.Magnitude.COMPLETE_INDEX -> "passed"
        TestStateInfo.Magnitude.FAILED_INDEX -> "failed"
        TestStateInfo.Magnitude.ERROR_INDEX -> "error"
        TestStateInfo.Magnitude.IGNORED_INDEX, TestStateInfo.Magnitude.SKIPPED_INDEX -> "skipped"
        else -> "finished"
    }

    /** "nearest": the raw element, exactly what a real click at that position would resolve through
     * (a producer already walks up to the enclosing method or class on its own). "class"/"file": the
     * enclosing test class, language-agnostically - the same finder go-to-test uses. */
    private fun anchorElement(mirror: Mirror, offset: Int, scope: String): PsiElement? {
        val file = PsiDocumentManager.getInstance(project).getPsiFile(mirror.document) ?: return null
        val at = file.findElementAt(offset) ?: file.findElementAt((offset - 1).coerceAtLeast(0)) ?: return null
        if (scope == "nearest") return at
        return TestFinderHelper.findSourceElement(at) ?: at
    }

    private fun finish(run: Run) {
        if (!run.finished.compareAndSet(false, true)) return
        run.output.flush() // every line before the end
        current.compareAndSet(run, null)
        val ms = (System.nanoTime() - run.started) / 1_000_000
        brain.record.info(run.transport.session, "test_run_finished") {
            put("runId", run.id); put("ok", run.succeeded && run.failure == null); put("cancelled", run.cancelled); put("ms", ms)
        }
        try {
            run.transport.send(Wire.notification("\$/ij/run/finished", buildJsonObject {
                put("runId", run.id)
                put("success", run.succeeded && run.failure == null && !run.cancelled)
                put("cancelled", run.cancelled)
                put("ms", ms)
                run.failure?.let { put("error", it) }
            }))
        } catch (_: Throwable) {
            // the Editor went away while it ran
        }
    }

    fun cancel(runId: String?): JsonElement {
        val run = current.get()?.takeIf { runId == null || it.id == runId }
        val stopped = run?.taskId?.let { id ->
            run.cancelled = true
            com.intellij.openapi.externalSystem.service.internal.ExternalSystemProcessingManager.getInstance().findTask(id)?.cancel() ?: false
        } ?: false
        return buildJsonObject { put("cancelled", stopped); run?.let { put("runId", it.id) } }
    }
}
