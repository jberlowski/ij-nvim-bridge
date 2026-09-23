package dev.bridge.brain

import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.externalSystem.model.ProjectKeys
import com.intellij.openapi.externalSystem.model.ProjectSystemId
import com.intellij.openapi.externalSystem.model.execution.ExternalSystemTaskExecutionSettings
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskId
import com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskNotificationListener
import com.intellij.openapi.externalSystem.service.execution.ProgressExecutionMode
import com.intellij.openapi.externalSystem.service.internal.ExternalSystemProcessingManager
import com.intellij.openapi.externalSystem.service.project.ProjectDataManager
import com.intellij.openapi.externalSystem.task.TaskCallback
import com.intellij.openapi.externalSystem.util.ExternalSystemApiUtil
import com.intellij.openapi.externalSystem.util.ExternalSystemUtil
import com.intellij.openapi.externalSystem.util.task.TaskExecutionSpec
import com.intellij.openapi.project.Project
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference

/**
 * Gradle tasks (FEATURES.md §6d): listed from the model IntelliJ has already imported, so nothing is run
 * to list them, and run through IntelliJ's own Gradle integration, so the project's Gradle settings, JVM and
 * wrapper are the project's own (Borrowed Settings).
 *
 * `$/ij/tasks` answers the hierarchy: projects (subprojects nested), each with its task groups. `$/ij/task/run`
 * starts a task and answers at once; its output follows as `$/ij/task/output` notifications and its end as
 * `$/ij/task/finished`. One task runs at a time; `$/ij/task/cancel` stops it.
 *
 * Only platform classes are used (the external-system API), so it needs no dependency on the Gradle plugin; without
 * one the model is simply empty.
 */
class GradleTasks(private val project: Project, private val brain: BrainService) {

    private val system = ProjectSystemId("GRADLE")

    private class Run(val id: String, val transport: Transport, val path: String, val tasks: List<String>) {
        val started = System.nanoTime()
        @Volatile var taskId: ExternalSystemTaskId? = null
        @Volatile var cancelled = false
        @Volatile var failure: String? = null
        @Volatile var succeeded = false
        val finished = java.util.concurrent.atomic.AtomicBoolean(false)
    }

    private val current = AtomicReference<Run?>()

    // ------------------------------------------------------------------ listing
    private class Node(val name: String, val path: String) {
        val tasks = java.util.TreeMap<String, Pair<String?, String?>>()      // name -> (group, description)
        val children = ArrayList<Node>()
    }

    /** Must run in a read action. */
    fun list(): JsonElement {
        val byPath = LinkedHashMap<String, Node>()
        for (info in ProjectDataManager.getInstance().getExternalProjectsData(project, system)) {
            val root = info.externalProjectStructure ?: continue
            for (module in ExternalSystemApiUtil.findAll(root, ProjectKeys.MODULE)) {
                val path = module.data.linkedExternalProjectPath
                val node = byPath.getOrPut(path) { Node(projectName(module.data.externalName, path), path) }
                for (task in ExternalSystemApiUtil.findAll(module, ProjectKeys.TASK)) {
                    node.tasks.putIfAbsent(task.data.name, task.data.group to task.data.description)
                }
            }
        }
        // Subprojects nest under the project whose directory contains theirs.
        val roots = ArrayList<Node>()
        for (node in byPath.values.sortedBy { it.path.length }) {
            val parent = byPath.values.filter { it !== node && node.path.startsWith(it.path.trimEnd('/') + "/") }
                .maxByOrNull { it.path.length }
            if (parent != null) parent.children += node else roots += node
        }
        return buildJsonObject {
            put("projects", JsonArray(roots.map(::json)))
            current.get()?.let { put("running", it.id) }
        }
    }

    /** A source set's module is `project.main`: the project is what is wanted. */
    private fun projectName(external: String, path: String): String =
        external.removeSuffix(".main").removeSuffix(".test").ifEmpty { path.substringAfterLast('/') }

    private fun json(node: Node): JsonObject = buildJsonObject {
        put("name", node.name)
        put("path", node.path)
        // The IDE shows a task with no group under "other".
        val groups = node.tasks.entries.groupBy { it.value.first?.takeIf { g -> g.isNotBlank() } ?: "other" }
        put("groups", JsonArray(groups.entries.sortedWith(compareBy({ it.key == "other" }, { it.key })).map { (group, tasks) ->
            buildJsonObject {
                put("name", group)
                put("tasks", JsonArray(tasks.map { (name, meta) ->
                    buildJsonObject {
                        put("name", name)
                        meta.second?.takeIf { it.isNotBlank() }?.let { put("description", it) }
                    }
                }))
            }
        }))
        put("projects", JsonArray(node.children.sortedBy { it.name }.map(::json)))
    }

    // ------------------------------------------------------------------ running
    /**
     * Starts the task(s) and answers at once with the run's id.
     * params: `path` (the Gradle project's directory), `tasks` (names), optional `args` (e.g. `--info`, `-Pkey=v`).
     */
    fun run(params: JsonObject, transport: Transport): JsonElement {
        val path = params["path"]?.jsonPrimitive?.contentOrNull ?: throw Rename.Refused("no project path given")
        val tasks = params["tasks"]?.jsonArray?.map { it.jsonPrimitive.content }
            ?: listOfNotNull(params["task"]?.jsonPrimitive?.contentOrNull)
        if (tasks.isEmpty()) throw Rename.Refused("no task given")
        val args = params["args"]?.jsonArray?.map { it.jsonPrimitive.content }.orEmpty()

        val run = Run(UUID.randomUUID().toString().take(8), transport, path, tasks)
        if (!current.compareAndSet(null, run)) {
            throw Rename.Refused("a Gradle task is already running (${current.get()?.tasks?.joinToString(" ")}): stop it first")
        }

        val settings = ExternalSystemTaskExecutionSettings().apply {
            externalProjectPath = path
            taskNames = tasks
            scriptParameters = args.joinToString(" ")
            externalSystemIdString = system.id
        }
        val listener = object : ExternalSystemTaskNotificationListener {
            override fun onStart(id: ExternalSystemTaskId) { run.taskId = id }
            override fun onStart(id: ExternalSystemTaskId, workingDir: String?) { run.taskId = id }
            // What is happening right now (e.g. "Executing task ':compileKotlin'") - the same signal
            // IntelliJ's own Gradle tool window shows as its live status line. Free: this listener
            // already carries it, just not forwarded before now.
            override fun onStatusChange(event: com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskNotificationEvent) {
                transport.send(Wire.notification("\$/ij/task/status", buildJsonObject {
                    put("runId", run.id); put("description", event.description)
                }))
            }
            override fun onTaskOutput(id: ExternalSystemTaskId, text: String, stdOut: Boolean) {
                transport.send(Wire.notification("\$/ij/task/output", buildJsonObject {
                    put("runId", run.id); put("text", text); put("stdout", stdOut)
                }))
            }
            override fun onSuccess(id: ExternalSystemTaskId) { run.succeeded = true }
            override fun onFailure(id: ExternalSystemTaskId, e: Exception) { run.failure = e.message ?: e::class.java.simpleName }
            override fun onCancel(id: ExternalSystemTaskId) { run.cancelled = true }
            override fun onEnd(id: ExternalSystemTaskId) { finish(run) }
        }
        val spec = TaskExecutionSpec.create(project, system, DefaultRunExecutor.EXECUTOR_ID, settings)
            .withProgressExecutionMode(ProgressExecutionMode.IN_BACKGROUND_ASYNC)
            .withListener(listener)
            .withCallback(object : TaskCallback {
                override fun onSuccess() { run.succeeded = true; later(run) }
                override fun onFailure() { later(run) }
            })
            .build()
        brain.record.info(transport.session, "gradle_run") { put("path", path); put("tasks", tasks.joinToString(" ")); put("runId", run.id) }
        ApplicationManager.getApplication().invokeLater {
            try {
                ExternalSystemUtil.runTask(spec)
            } catch (t: Throwable) {
                run.failure = "${t::class.java.simpleName}: ${t.message}"
                brain.record.error(transport.session, "gradle run", t)
                finish(run)
            }
        }
        return buildJsonObject { put("runId", run.id); put("path", path); put("tasks", JsonArray(tasks.map(::JsonPrimitive))) }
    }

    /** The callback can come before the last of the output: let the listener's end come first, and only if it does not, end here. */
    private fun later(run: Run) {
        java.util.concurrent.CompletableFuture.delayedExecutor(1500, java.util.concurrent.TimeUnit.MILLISECONDS).execute { finish(run) }
    }

    private fun finish(run: Run) {
        if (!run.finished.compareAndSet(false, true)) return
        current.compareAndSet(run, null)
        val ms = (System.nanoTime() - run.started) / 1_000_000
        brain.record.info(run.transport.session, "gradle_finished") {
            put("runId", run.id); put("ok", run.succeeded && run.failure == null); put("cancelled", run.cancelled); put("ms", ms)
        }
        try {
            run.transport.send(Wire.notification("\$/ij/task/finished", buildJsonObject {
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

    /** Stops the running task, if [runId] is it (or there is one and none is named). */
    fun cancel(runId: String?): JsonElement {
        val run = current.get()?.takeIf { runId == null || it.id == runId }
        val stopped = run?.taskId?.let { id ->
            run.cancelled = true
            ExternalSystemProcessingManager.getInstance().findTask(id)?.cancel() ?: false
        } ?: false
        return buildJsonObject { put("cancelled", stopped); run?.let { put("runId", it.id) } }
    }
}
