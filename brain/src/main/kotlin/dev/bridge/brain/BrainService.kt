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
    private var server: ServerSocketChannel? = null
    private var socketPath: Path? = null
    private var root: String? = null

    val mirrors = MirrorSet(project).also { Disposer.register(this, it) }
    val completion = CompletionEngine(project).also { Disposer.register(this, it) }

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
     * SPEC.md §8. Indexing covers the build-model import as well as dumb mode:
     * mid-import IntelliJ reports not-dumb while every reference is unresolved
     * (HARNESS.md §13). No source roots is the observable shadow of that; a
     * direct signal from the Gradle integration is still open (SPEC.md §14).
     */
    fun state(): String {
        val dumb = DumbService.getInstance(project).isDumb
        val hasRoots = ProjectRootManager.getInstance(project).contentSourceRoots.isNotEmpty()
        return if (dumb || !hasRoots) "Indexing" else "Ready"
    }

    fun capabilities(): JsonObject = buildJsonObject {
        // Passthrough: advertise only what this Brain has proven it can answer.
        put("completion", buildJsonObject { put("streaming", true) })
        put("diagnostics", false)
        put("formatting", false)
        put("ij", buildJsonObject { put("ide", ide()) })
    }

    fun projectName(): String = project.name

    /** Harness only (SPEC.md §10): is IntelliJ still showing a completion popup? */
    fun lookupActive(): Boolean = edt { LookupManager.getInstance(project).activeLookup != null }

    /** Harness only: provoke IntelliJ's Editor Tabs limit without a config edit. */
    fun setTabLimit(limit: Int) = edt { UISettings.getInstance().editorTabLimit = limit }
    fun ide(): String = ApplicationInfo.getInstance().build.asString()

    override fun dispose() {
        stopped.set(true)
        runCatching { server?.close() }
        socketPath?.let { runCatching { Files.deleteIfExists(it) } }
        root?.let { runCatching { Registry.withdraw(it) } }
    }

    private fun hash(value: String): String =
        MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
            .take(3).joinToString("") { "%02x".format(it) }
}
