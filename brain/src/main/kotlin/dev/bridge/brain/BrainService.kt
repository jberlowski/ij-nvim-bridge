package dev.bridge.brain

import com.intellij.openapi.Disposable
import com.intellij.codeInsight.lookup.LookupManager
import com.intellij.ide.ui.UISettings
import com.intellij.openapi.application.ApplicationInfo
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.DumbService
import com.intellij.openapi.project.Project
import com.intellij.openapi.roots.ProjectRootManager
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.util.Disposer
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.StandardProtocolFamily
import java.net.UnixDomainSocketAddress
import java.nio.channels.ServerSocketChannel
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean

class BrainStartup : ProjectActivity {
    override suspend fun execute(project: Project) {
        service<BridgeApp>().install()
        project.service<BrainService>().start()
    }
}

/**
 * One per open project: serves that project's unix socket and publishes it in
 * the Registry (SPEC.md §4.2). Everything a Session needs hangs off this.
 */
@Service(Service.Level.PROJECT)
class BrainService(private val project: Project) : Disposable {

    private val log = logger<BrainService>()
    private val stopped = AtomicBoolean(false)

    /** What happened, for finding out why (BridgeLog). Named after the project, so several IDE windows keep apart. */
    val record: BridgeLog = BridgeLog.forProject(hash(project.basePath ?: project.name))
    private var server: ServerSocketChannel? = null
    private var socketPath: Path? = null
    private var root: String? = null

    /** Save handshakes acknowledged: the Editor really ran §5.4 before a write. */
    val saveAcks = java.util.concurrent.atomic.AtomicInteger()

    /** Completion requests received, including degraded ones: proves the Editor asked again. */
    val completionRequests = java.util.concurrent.atomic.AtomicInteger()

    val mirrors = MirrorSet(project).also { Disposer.register(this, it) }
    val completion = CompletionEngine(project, record).also { Disposer.register(this, it) }
    val diagnostics = DiagnosticsPublisher(project, this).also { Disposer.register(this, it) }
    val formatting = Formatting(project)
    val navigation = NavigationEngine(project, this).also { Disposer.register(this, it) }
    val gradle = GradleTasks(project, this)
    val runConfigurations = RunConfigurations(project, this)
    val runnables = RunnablesPublisher(project, this).also { Disposer.register(this, it) }
    val testRunner = TestRunner(project, this)
    val carets = CaretFollower(this).also { Disposer.register(this, it) }
    val inlayHints = InlayHints(project, this).also {
        Disposer.register(this, it)
        mirrors.onOpened = { m -> it.watch(m); carets.watch(m) }
        mirrors.onReleased = { uri -> it.unwatch(uri); carets.unwatch(uri) }
    }

    init {
        mirrors.onGone = { uri, owners, why ->
            diagnostics.clear(uri); runnables.clear(uri)
            record.info(null, "file_gone") { put("uri", uri); put("why", why) }
            for (owner in owners) (owner as? Session)?.let { session ->
                session.notifyClient("\$/ij/fileGone", buildJsonObject { put("uri", uri); put("why", why) })
                session.notifyClient("window/showMessage", buildJsonObject {
                    put("type", 3) // Info
                    put("message", "${uri.substringAfterLast('/')}: $why. Neovim's text is kept; IntelliJ's help for it resumes when it is saved.")
                })
            }
        }
        mirrors.foreign.record = { uri, outcome -> record.info(null, "foreign_edit") { put("uri", uri); put("outcome", outcome) } }
    }

    private val transports = java.util.concurrent.CopyOnWriteArraySet<Transport>()

    fun register(transport: Transport) { transports += transport }
    fun unregister(transport: Transport) { transports -= transport }

    /** A notification for every connected Session. Mirrors are per project, not per Session. */
    fun broadcast(message: JsonObject) {
        for (t in transports) {
            try { t.send(message) } catch (_: Exception) { transports -= t }
        }
    }

    @Synchronized
    fun start() {
        if (server != null) return
        val root = project.basePath ?: run {
            log.warn("bridge: project has no basePath, not serving")
            return
        }
        this.root = root
        val dir = Registry.ensureDir()
        val sockName = hash(root) + ".sock"
        val path = dir.resolve(sockName)
        Files.deleteIfExists(path)

        val channel = ServerSocketChannel.open(StandardProtocolFamily.UNIX)
        channel.bind(UnixDomainSocketAddress.of(path))
        // Set explicitly rather than trusting the umask: anything group- or
        // world-accessible would let another account talk to the IDE.
        Files.setPosixFilePermissions(path, PosixFilePermissions.fromString("rw-------"))
        server = channel
        socketPath = path
        Registry.publish(root, sockName, ide())
        record.info(null, "brain_start") {
            put("root", root)
            put("socket", path.toString())
            put("ide", ide())
            put("plugin", pluginVersion())
            put("java", System.getProperty("java.version") ?: "?")
            put("os", "${System.getProperty("os.name")} ${System.getProperty("os.version")} ${System.getProperty("os.arch")}")
            put("level", record.level.name.lowercase())
        }
        watchStatus()
        log.info("bridge: serving $root on $path")

        Thread({ acceptLoop(channel) }, "bridge-accept-${project.name}").apply {
            isDaemon = true
            start()
        }
    }

    private fun acceptLoop(channel: ServerSocketChannel) {
        while (!stopped.get()) {
            val conn = try {
                channel.accept()
            } catch (t: Throwable) {
                if (!stopped.get()) log.warn("bridge: accept failed", t)
                return
            }
            Thread({ Session(conn, this).run() }, "bridge-session").apply {
                isDaemon = true
                start()
            }
        }
    }

    /**
     * SPEC.md §8. Indexing is any of: a build-model import in flight, IntelliJ's
     * own indexing (dumb mode), or a project model that is not loaded yet.
     *
     * The import matters as much as dumb mode. While it runs IntelliJ reports
     * not-dumb, yet every reference is unresolved and diagnostics read "Not
     * resolved until the project is fully loaded" (HARNESS.md §13). It is read
     * from the external-system layer directly rather than inferred.
     */
    fun status(): Status {
        val importing = try {
            com.intellij.openapi.externalSystem.service.internal.ExternalSystemProcessingManager.getInstance()
                .hasTaskOfTypeInProgress(
                    com.intellij.openapi.externalSystem.model.task.ExternalSystemTaskType.RESOLVE_PROJECT, project)
        } catch (_: Throwable) {
            false // no external-system layer in this IDE: nothing to import
        }
        if (importing) return Status("Indexing", "import")
        if (DumbService.getInstance(project).isDumb) return Status("Indexing", "indexing")
        if (ProjectRootManager.getInstance(project).contentSourceRoots.isEmpty()) return Status("Indexing", "model")
        return Status("Ready", null)
    }

    fun state(): String = status().state

    data class Status(val state: String, val reason: String?) {
        fun toJson(): JsonObject = buildJsonObject {
            put("state", state)
            if (reason != null) put("reason", reason)
        }
    }

    /** Announce every change of state to every Session, and re-publish what was withheld. */
    private fun watchStatus() {
        var last: Status? = null
        monitor.scheduleWithFixedDelay({
            try {
                val now = status()
                if (now == last) return@scheduleWithFixedDelay
                val before = last
                last = now
                record.info(null, "status") {
                    put("state", now.state)
                    now.reason?.let { put("reason", it) }
                }
                broadcast(Wire.notification("\$/ij/status", now.toJson()))
                if (now.state == "Ready" && before != null) {
                    diagnostics.republishAll()
                    mirrors.all().forEach { runnables.schedule(it.uri) }
                }
            } catch (t: Throwable) {
                log.warn("bridge: status check failed", t)
                record.error(null, "status check", t)
            }
        }, 0, 400, java.util.concurrent.TimeUnit.MILLISECONDS)
    }

    private val monitor = java.util.concurrent.Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "bridge-status").apply { isDaemon = true }
    }

    /**
     * Pick up files created, moved or deleted on disk behind the IDE's back: an IDE that is
     * not the active application does not look by itself, and the Editor writes the files.
     * Asynchronous unless the caller (the harness) needs to know it is done.
     */
    fun refreshFiles(async: Boolean = false) {
        val root = project.basePath ?: return
        com.intellij.openapi.vfs.LocalFileSystem.getInstance().refreshAndFindFileByPath(root)?.let {
            com.intellij.openapi.vfs.VfsUtil.markDirtyAndRefresh(async, true, true, it)
        }
    }

    /** Harness only: put IntelliJ in dumb mode for [ms] so Indexing can be observed. */
    fun simulateIndexing(ms: Long) {
        com.intellij.openapi.project.DumbService.getInstance(project).queueTask(
            object : com.intellij.openapi.project.DumbModeTask() {
                override fun performInDumbMode(indicator: com.intellij.openapi.progress.ProgressIndicator) {
                    Thread.sleep(ms)
                }
            })
    }

    fun capabilities(): JsonObject = buildJsonObject {
        // Passthrough: advertise only what this Brain has proven it can answer.
        // Standard LSP, so Neovim's own client does the syncing: without
        // textDocumentSync it sends no didChange, and without willSaveWaitUntil
        // it never runs the save handshake (SPEC.md §5.4).
        put("positionEncoding", "utf-16")
        put("textDocumentSync", buildJsonObject {
            put("openClose", true)
            put("change", 2) // incremental
            put("willSave", false)
            put("willSaveWaitUntil", true)
            put("save", buildJsonObject { put("includeText", false) })
        })
        // Standard LSP navigation: Neovim's own gd, gy, gI, grr, K and highlights.
        put("definitionProvider", true)
        put("declarationProvider", true)
        put("typeDefinitionProvider", true)
        put("implementationProvider", true)
        put("referencesProvider", true)
        put("hoverProvider", true)
        put("documentHighlightProvider", true)
        put("documentSymbolProvider", true)
        put("workspaceSymbolProvider", true)
        put("foldingRangeProvider", true)
        put("selectionRangeProvider", true)
        put("codeActionProvider", buildJsonObject {
            put("codeActionKinds", kotlinx.serialization.json.JsonArray(
                CodeActions.KINDS.map { kotlinx.serialization.json.JsonPrimitive(it) }))
            put("resolveProvider", true) // titles now, edits on resolve (FEATURES.md D3)
        })
        put("renameProvider", buildJsonObject { put("prepareProvider", true) })
        put("inlayHintProvider", buildJsonObject { put("resolveProvider", false) })
        put("tasks", buildJsonObject { put("gradle", true) })
        put("testNavigation", true) // $/ij/testTargets: an extension, LSP has no "go to test" method
        put("testRunning", true) // $/ij/runnables, $/ij/run, $/ij/run/cancel: extensions, no LSP equivalent
        put("runConfigurations", true) // $/ij/runConfigurations, $/ij/runConfiguration/run and /cancel
        put("workspace", buildJsonObject {
            put("fileOperations", buildJsonObject {
                val filters = kotlinx.serialization.json.JsonArray(listOf(buildJsonObject {
                    put("scheme", "file")
                    put("pattern", buildJsonObject { put("glob", "**/*.{kt,java}"); put("matches", "file") })
                }))
                // Asked before a move, to say what else changes; told after, so the IDE looks at the disk.
                put("willRename", buildJsonObject { put("filters", filters) })
                put("didRename", buildJsonObject { put("filters", filters) })
                put("didCreate", buildJsonObject { put("filters", filters) })
                put("didDelete", buildJsonObject { put("filters", filters) })
            })
        })
        put("documentFormattingProvider", true)
        put("documentRangeFormattingProvider", true)
        put("signatureHelpProvider", buildJsonObject {
            put("triggerCharacters", kotlinx.serialization.json.JsonArray(listOf("(", ",").map { kotlinx.serialization.json.JsonPrimitive(it) }))
            put("retriggerCharacters", kotlinx.serialization.json.JsonArray(listOf(kotlinx.serialization.json.JsonPrimitive(","))))
        })
        put("completion", buildJsonObject { put("streaming", true); put("resolve", true) })
        put("diagnostics", true)
        put("formatting", false)
        put("ij", buildJsonObject { put("ide", ide()) })
    }

    /** The Project Root this Brain serves. */
    fun projectRoot(): String? = root

    fun projectName(): String = project.name

    /** Harness only (SPEC.md §10): is IntelliJ still showing a completion popup? */
    fun lookupActive(): Boolean = edt { LookupManager.getInstance(project).activeLookup != null }

    /** Harness only: provoke IntelliJ's Editor Tabs limit without a config edit. */
    fun setTabLimit(limit: Int) = edt { UISettings.getInstance().editorTabLimit = limit }
    fun ide(): String = ApplicationInfo.getInstance().build.asString()

    override fun dispose() {
        monitor.shutdownNow()
        stopped.set(true)
        runCatching { server?.close() }
        socketPath?.let { runCatching { Files.deleteIfExists(it) } }
        root?.let { runCatching { Registry.withdraw(it) } }
    }

    private fun hash(value: String): String =
        MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
            .take(3).joinToString("") { "%02x".format(it) }
}

/** The Brain's own version, from its plugin descriptor: `version` in `brain/build.gradle.kts` is the one place it is written. */
fun pluginVersion(): String =
    com.intellij.ide.plugins.PluginManagerCore.getPlugin(com.intellij.openapi.extensions.PluginId.getId("dev.bridge.brain"))?.version ?: "unknown"
