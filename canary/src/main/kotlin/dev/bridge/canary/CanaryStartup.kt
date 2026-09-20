package dev.bridge.canary

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.DumbService
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.vfs.VirtualFileManager
import java.net.StandardProtocolFamily
import java.net.UnixDomainSocketAddress
import java.nio.channels.Channels
import java.nio.channels.ServerSocketChannel
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Serves one unix socket per open project and publishes the Registry, exactly
 * as SPEC.md §4.2 describes.
 *
 * This is a probe, not the Bridge. It answers only enough to prove the harness
 * can reach into a running IDE, learn its state, and measure a round trip.
 */
class CanaryStartup : ProjectActivity {

    private val log = logger<CanaryStartup>()

    override suspend fun execute(project: Project) {
        val root = project.basePath ?: run {
            log.warn("canary: project has no basePath, not serving")
            return
        }
        val server = CanaryServer(project, root)
        server.start()
    }
}

class CanaryServer(private val project: Project, private val root: String) {

    private val log = logger<CanaryServer>()
    private val stopped = AtomicBoolean(false)

    fun start() {
        val dir = registryDir()
        Files.createDirectories(dir)
        val sockName = hash(root) + ".sock"
        val sockPath = dir.resolve(sockName)
        Files.deleteIfExists(sockPath)

        val channel = ServerSocketChannel.open(StandardProtocolFamily.UNIX)
        channel.bind(UnixDomainSocketAddress.of(sockPath))
        // Unprivileged end to end (SPEC.md §3): owner-only, under the user's
        // own runtime dir, no daemon and no privileged port anywhere.
        // Set explicitly rather than trusting the process umask — anything
        // group- or world-accessible would let another account talk to the IDE.
        Files.setPosixFilePermissions(sockPath, PosixFilePermissions.fromString("rw-------"))
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwx------"))

        publishRegistry(dir, sockName)
        log.info("canary: serving $root on $sockPath")

        Thread({ acceptLoop(channel) }, "canary-accept-${project.name}").apply {
            isDaemon = true
            start()
        }
    }

    private fun acceptLoop(server: ServerSocketChannel) {
        while (!stopped.get()) {
            val conn = try {
                server.accept()
            } catch (t: Throwable) {
                if (!stopped.get()) log.warn("canary: accept failed", t)
                return
            }
            Thread({ serve(conn) }, "canary-conn").apply { isDaemon = true; start() }
        }
    }

    private fun serve(conn: java.nio.channels.SocketChannel) {
        conn.use {
            val input = Channels.newInputStream(conn)
            val output = Channels.newOutputStream(conn)
            while (true) {
                val msg = Rpc.readMessage(input) ?: return
                // t1 of SPEC.md §7 is when the request has arrived, not when we
                // began waiting for it. Stamping before the blocking read put
                // the client's own connect and send time into "in-IDE" time,
                // which could exceed the round trip and make OVERHEAD negative.
                val received = System.nanoTime()
                val method = Rpc.stringField(msg, "method") ?: "?"
                val id = Rpc.stringField(msg, "id") ?: "0"
                // Never let a throwing handler close the connection silently:
                // the client would block until its timeout and report a hang
                // instead of the actual error. A visible failure beats a stall.
                val result = try {
                    dispatch(method, received)
                } catch (t: Throwable) {
                    log.warn("canary: $method failed", t)
                    Rpc.obj(
                        "error" to Rpc.quote("${t::class.java.name}: ${t.message}"),
                        "stack" to Rpc.quote(
                            t.stackTrace.take(6).joinToString(" | ") { it.toString() }
                        ),
                    )
                }
                Rpc.writeMessage(
                    output,
                    Rpc.obj(
                        "jsonrpc" to Rpc.quote("2.0"),
                        "id" to Rpc.quote(id),
                        "result" to result,
                    ),
                )
            }
        }
    }

    private fun dispatch(method: String, receivedNanos: Long): String = when (method) {
        "\$/canary/ping" -> Rpc.obj(
            "pong" to "true",
            "tReceivedNanos" to receivedNanos.toString(),
            "tRepliedNanos" to System.nanoTime().toString(),
        )

        "\$/canary/state" -> state(receivedNanos)

        // Spike question 1 (SPEC.md §11): can the plugin open a real editor?
        "\$/canary/openEditor" -> openEditor(receivedNanos)

        else -> Rpc.obj("error" to Rpc.quote("unknown method: $method"))
    }

    private fun state(receivedNanos: Long): String {
        val dumb = DumbService.getInstance(project).isDumb
        val editors = readAction {
            FileEditorManager.getInstance(project).openFiles.map { Rpc.quote(it.path) }
        }
        return Rpc.obj(
            "project" to Rpc.quote(project.name),
            "root" to Rpc.quote(root),
            // The Indexing state of SPEC.md §8, read from the live IDE.
            "indexing" to dumb.toString(),
            "openFiles" to Rpc.arr(editors),
            "ide" to Rpc.quote(
                com.intellij.openapi.application.ApplicationInfo.getInstance().build.asString()
            ),
            "tReceivedNanos" to receivedNanos.toString(),
            "tRepliedNanos" to System.nanoTime().toString(),
        )
    }

    /**
     * Opens a file as a real editor and reports whether IntelliJ gave us one.
     * Deliberately the cheapest possible probe of ADR-0001's premise.
     */
    private fun openEditor(receivedNanos: Long): String {
        val target = Path.of(root, RELATIVE_PROBE_FILE)
        val vf = VirtualFileManager.getInstance().findFileByNioPath(target)
            ?: return Rpc.obj(
                "opened" to "false",
                "reason" to Rpc.quote("virtual file not found: $target"),
            )

        // Opening an editor must happen on the EDT, in a write-safe context.
        // The modality state is explicit: a bare invokeAndWait from a socket
        // thread can land in the wrong context and throw.
        var editor: Editor? = null
        var failure: Throwable? = null
        ApplicationManager.getApplication().invokeAndWait({
            try {
                editor = FileEditorManager.getInstance(project).openTextEditor(
                    OpenFileDescriptor(project, vf, 0), /* focusEditor = */ false
                )
            } catch (t: Throwable) {
                failure = t
            }
        }, ModalityState.nonModal())

        failure?.let {
            return Rpc.obj(
                "opened" to "false",
                "reason" to Rpc.quote("${it::class.java.name}: ${it.message}"),
            )
        }
        val e = editor
        return Rpc.obj(
            "opened" to (e != null).toString(),
            "file" to Rpc.quote(vf.path),
            "documentLength" to (e?.document?.textLength ?: -1).toString(),
            "tReceivedNanos" to receivedNanos.toString(),
            "tRepliedNanos" to System.nanoTime().toString(),
        )
    }

    private fun <T> readAction(block: () -> T): T =
        ApplicationManager.getApplication().runReadAction<T>(block)

    private fun publishRegistry(dir: Path, sockName: String) {
        val entry = Rpc.obj(
            "root" to Rpc.quote(root),
            "sock" to Rpc.quote(sockName),
            "pid" to ProcessHandle.current().pid().toString(),
            "ide" to Rpc.quote(
                com.intellij.openapi.application.ApplicationInfo.getInstance().build.asString()
            ),
        )
        val registry = Rpc.obj("version" to "1", "brains" to Rpc.arr(listOf(entry)))
        // A single-project canary rewrites the file wholesale. The Bridge will
        // need to merge, since one IDE serves several projects.
        Files.writeString(dir.resolve("registry.json"), registry)
    }

    private fun registryDir(): Path {
        val runtime = System.getenv("XDG_RUNTIME_DIR")
        return if (!runtime.isNullOrBlank()) Path.of(runtime, "ij-nvim-bridge")
        // SPEC.md §4.2: the fallback is not optional — XDG_RUNTIME_DIR is
        // unset on WSL without systemd.
        else Path.of(System.getProperty("user.home"), ".ij-nvim-bridge")
    }

    private fun hash(value: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
        return digest.take(3).joinToString("") { "%02x".format(it) }
    }

    companion object {
        const val RELATIVE_PROBE_FILE =
            "src/main/kotlin/dev/bridge/fixture/web/GreetingController.kt"
    }
}
