package dev.bridge.brain

import com.intellij.openapi.externalSystem.model.ProjectSystemId
import com.intellij.openapi.externalSystem.util.ExternalSystemApiUtil
import com.intellij.openapi.project.Project
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.net.URI
import java.nio.file.Path

/** Separates the IntelliJ workspace from its independently linked builds. No filesystem build guessing. */
class ProjectContext(private val project: Project) {
    fun roots(): List<String> = ExternalSystemApiUtil.getSettings(project, ProjectSystemId("GRADLE"))
        .linkedProjectsSettings.map { canonical(it.externalProjectPath) }.distinct().sorted()

    fun describe(): JsonObject = buildJsonObject {
        put("name", project.name)
        put("root", project.basePath ?: "")
        put("builds", JsonArray(roots().map { root -> buildJsonObject {
            put("path", root); put("name", Path.of(root).fileName.toString())
        } }))
    }

    /** Null for context-free legacy requests; refuse contextual requests outside any imported build. */
    fun build(params: JsonObject): String? {
        val uri = params["textDocument"]?.jsonObject?.get("uri")?.jsonPrimitive?.contentOrNull ?: return null
        val path = canonical(Path.of(URI(uri)).toString())
        return roots().filter { contains(it, path) }.maxByOrNull { it.length }
            ?: throw Rename.Refused("no imported Gradle build owns $path")
    }

    fun owning(path: String): String? {
        val normalized = canonical(path)
        return roots().filter { contains(it, normalized) }.maxByOrNull { it.length }
    }

    companion object {
        fun canonical(path: String): String {
            val p = Path.of(path).toAbsolutePath().normalize()
            return try { p.toRealPath().toString() } catch (_: java.io.IOException) { p.toString() }
        }
        fun contains(root: String, path: String): Boolean = path == root || path.startsWith(root.trimEnd('/') + "/")
    }
}
