package dev.bridge.brain

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.PosixFilePermissions

/**
 * The Registry of SPEC.md §4.2: maps each Project Root to the socket serving it.
 *
 * One IDE serves several projects, so unlike the canary this merges rather than
 * rewrites, and it prunes entries whose process is gone. All IDE processes share
 * one file, so writes are atomic (temp + rename).
 */
object Registry {

    fun dir(): Path {
        val runtime = System.getenv("XDG_RUNTIME_DIR")
        // SPEC.md §4.2: the fallback is not optional; XDG_RUNTIME_DIR is unset
        // on WSL without systemd.
        return if (!runtime.isNullOrBlank()) Path.of(runtime, "ij-nvim-bridge")
        else Path.of(System.getProperty("user.home"), ".ij-nvim-bridge")
    }

    /** Creates the directory owner-only. Unprivileged end to end (SPEC.md §3). */
    fun ensureDir(): Path {
        val dir = dir()
        Files.createDirectories(dir)
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwx------"))
        return dir
    }

    @Synchronized
    fun publish(root: String, sock: String, ide: String) {
        val entries = load().filter { it.root != root && alive(it.pid) } + Entry(
            root, sock, ProcessHandle.current().pid(), ide
        )
        store(entries)
    }

    @Synchronized
    fun withdraw(root: String) {
        val mine = ProcessHandle.current().pid()
        store(load().filter { !(it.root == root && it.pid == mine) && alive(it.pid) })
    }

    private data class Entry(val root: String, val sock: String, val pid: Long, val ide: String)

    private fun load(): List<Entry> {
        val file = dir().resolve("registry.json")
        if (!Files.exists(file)) return emptyList()
        return try {
            Wire.json.parseToJsonElement(Files.readString(file)).jsonObject["brains"]
                ?.jsonArray?.map {
                    val o = it.jsonObject
                    Entry(
                        o["root"]!!.jsonPrimitive.content,
                        o["sock"]!!.jsonPrimitive.content,
                        o["pid"]!!.jsonPrimitive.longOrNull ?: 0,
                        o["ide"]?.jsonPrimitive?.contentOrNull ?: "",
                    )
                } ?: emptyList()
        } catch (_: Exception) {
            emptyList() // a torn or foreign file is rewritten, not trusted
        }
    }

    private fun store(entries: List<Entry>) {
        val dir = ensureDir()
        val doc: JsonObject = buildJsonObject {
            put("version", 1)
            put("brains", JsonArray(entries.map {
                buildJsonObject {
                    put("root", it.root); put("sock", it.sock)
                    put("pid", it.pid); put("ide", it.ide)
                }
            }))
        }
        val tmp = Files.createTempFile(dir, "registry", ".tmp")
        Files.writeString(tmp, Wire.json.encodeToString(JsonObject.serializer(), doc))
        Files.move(tmp, dir.resolve("registry.json"), StandardCopyOption.ATOMIC_MOVE,
            StandardCopyOption.REPLACE_EXISTING)
    }

    private fun alive(pid: Long): Boolean = ProcessHandle.of(pid).map { it.isAlive }.orElse(false)
}
