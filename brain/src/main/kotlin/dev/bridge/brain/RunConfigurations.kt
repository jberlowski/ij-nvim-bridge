package dev.bridge.brain

import com.intellij.execution.RunManager
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.externalSystem.model.ProjectSystemId
import com.intellij.openapi.externalSystem.model.execution.ExternalSystemTaskExecutionSettings
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskId
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskNotificationEvent
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskNotificationListener
import com.intellij.openapi.externalSystem.service.execution.ExternalSystemRunConfiguration
import com.intellij.openapi.externalSystem.service.execution.ProgressExecutionMode
import com.intellij.openapi.externalSystem.service.internal.ExternalSystemProcessingManager
import com.intellij.openapi.externalSystem.util.ExternalSystemUtil
import com.intellij.openapi.externalSystem.util.task.TaskExecutionSpec
import com.intellij.openapi.project.Project
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * IntelliJ Run Configurations (FEATURES.md §6c): a developer's own named, saved way to run
 * something (`RunManager`), possibly checked into the `.run` folder (`name.run.xml`) and so shared
 * through the repo - distinct from a raw Gradle task (`GradleTasks`, §5k) and from `$/ij/run`'s ad
 * hoc, per-position resolution for a test (`TestRunner`, §5n). `$/ij/runConfigurations` lists every
 * one IntelliJ knows about (in-IDE and `.run`-persisted alike - both surface identically through
 * `RunManager`, confirmed by reproducing a real checked-in `.run` file against the harness before
 * writing any of this: no reload lever needed, a plain VFS refresh is enough).
 *
 * **v1 runs only Gradle-backed configurations.** Their own `taskNames`/`scriptParameters`/
 * `externalProjectPath` are extracted and driven through `ExternalSystemUtil.runTask`, exactly the
 * machinery `GradleTasks` and `TestRunner` already proved reliable - so a Gradle-backed Run
 * Configuration is a checked-in, named, repeatable version of "run a Gradle task with specific
 * parameters".
 *
 * **A plain JVM Application configuration is a real, distinct, unfinished gap - not attempted
 * silently.** Reproducing it against the real IDE found every generic execution entry point tried
 * (`ProgramRunnerUtil.executeConfiguration`, the lower-level `ExecutionManager.restartRunProfile`)
 * does nothing at all once a "Make before launch" step is ruled out (that step hangs indefinitely on
 * this project's own deliberately-broken diagnostics probe, `ResolutionError.kt`, which IntelliJ's
 * own incremental compiler does not exclude the way Gradle's build script does - a separate, real
 * finding in itself). No process starts, no `ExecutionListener` callback fires, no exception is
 * thrown - the same silent-no-op family as Kotlin's own Inline (§5o) and Move Members (D2). Not
 * chased further for now; refused with a clear reason instead of a silent no-op of our own.
 */
class RunConfigurations(private val project: Project, private val brain: BrainService) {

    private class Run(val id: String, val transport: Transport, val name: String) {
        val started = System.nanoTime()
        @Volatile var taskId: ExternalSystemTaskId? = null
        @Volatile var cancelled = false
        @Volatile var failure: String? = null
        @Volatile var succeeded = false
        val finished = AtomicBoolean(false)
    }

    private val current = AtomicReference<Run?>()

    /** Must run in a read action. */
    fun list(): JsonElement = buildJsonObject {
        val all = RunManager.getInstance(project).allSettings
        put("configurations", JsonArray(all.map { s ->
            buildJsonObject {
                put("name", s.name)
                put("type", s.type.displayName)
                put("gradle", s.configuration is ExternalSystemRunConfiguration)
            }
        }))
        current.get()?.let { put("running", it.name) }
    }

    fun run(params: JsonObject, transport: Transport): JsonElement {
        val name = params["name"]?.jsonPrimitive?.contentOrNull ?: throw Rename.Refused("no configuration name given")
        val settings = RunManager.getInstance(project).findConfigurationByName(name)
            ?: throw Rename.Refused("no run configuration named '$name'")
        val es = settings.configuration as? ExternalSystemRunConfiguration
            ?: throw Rename.Refused("'$name' is not a Gradle-backed configuration; only those can be run so far")

        val run = Run(UUID.randomUUID().toString().take(8), transport, name)
        if (!current.compareAndSet(null, run)) {
            throw Rename.Refused("a run configuration is already running (${current.get()?.name}): stop it first")
        }

        val gsettings = ExternalSystemTaskExecutionSettings().apply {
            externalProjectPath = es.settings.externalProjectPath
            taskNames = es.settings.taskNames
            scriptParameters = es.settings.scriptParameters
            externalSystemIdString = "GRADLE"
        }
        val listener = object : ExternalSystemTaskNotificationListener {
            override fun onStart(id: ExternalSystemTaskId) { run.taskId = id }
            override fun onStart(id: ExternalSystemTaskId, workingDir: String?) { run.taskId = id }
            override fun onStatusChange(event: ExternalSystemTaskNotificationEvent) {
                run.transport.send(Wire.notification("\$/ij/runConfiguration/status", buildJsonObject {
                    put("runId", run.id); put("description", event.description)
                }))
            }
            override fun onTaskOutput(id: ExternalSystemTaskId, text: String, stdOut: Boolean) {
                run.transport.send(Wire.notification("\$/ij/runConfiguration/output", buildJsonObject {
                    put("runId", run.id); put("text", text); put("stdout", stdOut)
                }))
            }
            override fun onSuccess(id: ExternalSystemTaskId) { run.succeeded = true }
            override fun onFailure(id: ExternalSystemTaskId, e: Exception) { run.failure = e.message ?: e::class.java.simpleName }
            override fun onCancel(id: ExternalSystemTaskId) { run.cancelled = true }
            override fun onEnd(id: ExternalSystemTaskId) { finish(run) }
        }
        val spec = TaskExecutionSpec.create(project, ProjectSystemId("GRADLE"), DefaultRunExecutor.EXECUTOR_ID, gsettings)
            .withProgressExecutionMode(ProgressExecutionMode.IN_BACKGROUND_ASYNC)
            .withListener(listener)
            .build()
        brain.record.info(transport.session, "run_configuration") { put("name", name); put("runId", run.id) }
        ApplicationManager.getApplication().invokeLater {
            try {
                ExternalSystemUtil.runTask(spec)
            } catch (t: Throwable) {
                run.failure = "${t::class.java.simpleName}: ${t.message}"
                brain.record.error(transport.session, "run configuration", t)
                finish(run)
            }
        }
        return buildJsonObject { put("runId", run.id); put("name", name) }
    }

    private fun finish(run: Run) {
        if (!run.finished.compareAndSet(false, true)) return
        current.compareAndSet(run, null)
        val ms = (System.nanoTime() - run.started) / 1_000_000
        brain.record.info(run.transport.session, "run_configuration_finished") {
            put("runId", run.id); put("ok", run.succeeded && run.failure == null); put("cancelled", run.cancelled); put("ms", ms)
        }
        try {
            run.transport.send(Wire.notification("\$/ij/runConfiguration/finished", buildJsonObject {
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

    /** Stops the running one, if [runId] is it (or there is one and none is named). */
    fun cancel(runId: String?): JsonElement {
        val run = current.get()?.takeIf { runId == null || it.id == runId }
        val stopped = run?.taskId?.let { id ->
            run.cancelled = true
            ExternalSystemProcessingManager.getInstance().findTask(id)?.cancel() ?: false
        } ?: false
        return buildJsonObject { put("cancelled", stopped); run?.let { put("runId", it.id) } }
    }
}
