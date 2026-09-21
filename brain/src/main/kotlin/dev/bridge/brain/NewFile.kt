package dev.bridge.brain

import com.intellij.ide.fileTemplates.FileTemplate
import com.intellij.ide.fileTemplates.FileTemplateManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.roots.ProjectRootManager
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.net.URI
import java.nio.file.Files
import java.nio.file.Path

/**
 * `$/ij/newFile`: a new class, interface, enum or record, from IntelliJ's own file
 * templates, with the right `package` line (FEATURES.md §5).
 *
 * Plain LSP has no "create from a template" request, so this is an extension. Data first:
 * the Brain returns the *text* and where it belongs; it creates nothing. The Editor writes
 * the file, because Neovim owns the bytes, and the first write is what makes the IDE see it.
 * The package comes from the directory, relative to the source root that contains it, as
 * IntelliJ derives it; the directory need not exist yet.
 */
class NewFile(private val project: Project) {

    /** Template names, by language and kind, as IntelliJ names them internally. */
    private val templates = mapOf(
        "java" to mapOf(
            "class" to listOf("Class"), "interface" to listOf("Interface"), "enum" to listOf("Enum"),
            "record" to listOf("Record"), "annotation" to listOf("AnnotationType"),
        ),
        "kotlin" to mapOf(
            "class" to listOf("Kotlin Class"), "interface" to listOf("Kotlin Interface"),
            "enum" to listOf("Kotlin Enum"), "object" to listOf("Kotlin Object", "Kotlin Class"),
            "file" to listOf("Kotlin File"), "dataClass" to listOf("Kotlin Class"),
        ),
    )

    fun create(params: JsonObject): JsonElement {
        fun text(key: String) = params[key]?.jsonPrimitive?.contentOrNull
        val name = text("name")?.trim().orEmpty()
        val kind = text("template") ?: "class"
        val language = (text("language") ?: "kotlin").lowercase()
        val directory = text("directory")?.let { Path.of(URI(it)) } ?: throw Rename.Refused("no directory given")

        if (name.isEmpty() || !Character.isJavaIdentifierStart(name[0]) || !name.all { Character.isJavaIdentifierPart(it) }) {
            throw Rename.Refused("'$name' is not a valid name for a class")
        }
        val byKind = templates[language] ?: throw Rename.Refused("no templates for '$language' (java, kotlin)")
        val candidates = byKind[kind] ?: throw Rename.Refused("no '$kind' template for $language (${byKind.keys.joinToString()})")
        val extension = if (language == "java") "java" else "kt"
        val target = directory.resolve("$name.$extension")
        if (Files.exists(target)) throw Rename.Refused("${target.fileName} already exists")

        val pkg = packageOf(directory) ?: throw Rename.Refused("$directory is not under a source root of this project")

        val manager = FileTemplateManager.getInstance(project)
        val template = candidates.firstNotNullOfOrNull { manager.getInternalTemplate(it) ?: manager.getTemplate(it) }
            ?: throw Rename.Refused("this IDE has no file template named ${candidates.joinToString(" or ")}")
        val properties = manager.defaultProperties.apply {
            setProperty(FileTemplate.ATTRIBUTE_NAME, name)
            setProperty(FileTemplate.ATTRIBUTE_PACKAGE_NAME, pkg)
        }
        var body = template.getText(properties).replace("\r\n", "\n").replace('\r', '\n')
        if (kind == "dataClass") body = body.replaceFirst("class $name", "data class $name(val value: String)")
        if (!body.endsWith("\n")) body += "\n"

        return buildJsonObject {
            put("uri", target.toUri().toString())
            put("text", body)
            put("package", pkg)
            put("template", candidates.first())
        }
    }

    /** The package of [directory]: its path below the source root that contains it; null if none does. */
    private fun packageOf(directory: Path): String? {
        val roots = ProjectRootManager.getInstance(project).contentSourceRoots.map { Path.of(it.path) }
        val root = roots.firstOrNull { directory.startsWith(it) } ?: return null
        return root.relativize(directory).joinToString(".") { it.toString() }
    }
}
