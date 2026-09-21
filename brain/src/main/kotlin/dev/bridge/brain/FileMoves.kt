package dev.bridge.brain

import com.intellij.openapi.project.Project
import com.intellij.openapi.roots.ProjectFileIndex
import com.intellij.openapi.roots.ProjectRootManager
import com.intellij.openapi.util.TextRange
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiManager
import com.intellij.psi.PsiNamedElement
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.psi.search.searches.ReferencesSearch
import com.intellij.psi.util.PsiTreeUtil
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.net.URI
import java.nio.file.Path

/**
 * `workspace/willRenameFiles`: moving a Java or Kotlin file to another directory
 * changes its package, and everything that named it must follow (FEATURES.md §5).
 *
 * Data first (D2): the answer is a `WorkspaceEdit` against the files as they are now,
 * which the client applies *before* it moves the file. It carries:
 *  - the moved file's `package` line;
 *  - every explicit import and every fully qualified name of what the file declares;
 *  - an import where a name used to resolve by being in the same package or under a
 *    star import of the old one, and no longer will - in the files that use the moved
 *    declarations, and in the moved file for what it used from the old package.
 *
 * What it does not do, yet: move a directory (a package rename), keep imports sorted
 * (new ones are appended to the list; Organize Imports sorts them), or handle several
 * files moved together that refer to one another.
 */
class FileMoves(private val project: Project, private val locations: Locations) {

    private class Edit(val file: PsiFile, val range: TextRange, val text: String)

    /** Must run in a read action. */
    fun willRename(params: JsonObject): JsonElement {
        val edits = ArrayList<Edit>()
        val imports = LinkedHashMap<PsiFile, java.util.TreeSet<String>>()
        for (move in params["files"]?.jsonArray.orEmpty()) {
            val old = move.jsonObject["oldUri"]?.jsonPrimitive?.content ?: continue
            val new = move.jsonObject["newUri"]?.jsonPrimitive?.content ?: continue
            planMove(old, new, edits, imports)
        }
        for ((file, names) in imports) importInsertion(file, names, edits)

        val byUri = LinkedHashMap<String, MutableList<Pair<JsonObject, String>>>()
        val seen = HashSet<String>()
        for (e in edits) {
            val at = locations.locate(e.file, e.range) ?: continue
            val uri = (at["uri"] as JsonPrimitive).content
            val range = at["range"] as JsonObject
            if (seen.add("$uri$range${e.text}")) byUri.getOrPut(uri) { ArrayList() } += range to e.text
        }
        return buildJsonObject {
            put("changes", buildJsonObject {
                for ((uri, list) in byUri) put(uri, JsonArray(list.map { (range, text) ->
                    buildJsonObject { put("range", range); put("newText", text) }
                }))
            })
        }
    }

    // ---------------------------------------------------------------- one move
    private fun planMove(oldUri: String, newUri: String, edits: MutableList<Edit>,
                         imports: MutableMap<PsiFile, java.util.TreeSet<String>>) {
        val oldPath = Path.of(URI(oldUri))
        val newPath = Path.of(URI(newUri))
        val virtual = LocalFileSystem.getInstance().findFileByNioFile(oldPath) ?: return
        val moved = PsiManager.getInstance(project).findFile(virtual) ?: return
        if (moved.language.id != "JAVA" && moved.language.id != "kotlin") return

        val oldPackage = packageOf(moved)
        val newPackage = packageFor(newPath.parent, virtual) ?: return
        if (newPackage == oldPackage) return

        packageEdit(moved, newPackage)?.let { edits += it }

        fun qualified(pkg: String, name: String) = if (pkg.isEmpty()) name else "$pkg.$name"
        val declared = moved.children.filterIsInstance<PsiNamedElement>().filter { !it.name.isNullOrEmpty() }

        // What names the moved file: who imports it, spells it out, or used it by being next door.
        for (declaration in declared) {
            val name = declaration.name!!
            val oldName = qualified(oldPackage, name)
            val newName = qualified(newPackage, name)
            for (reference in ReferencesSearch.search(declaration, GlobalSearchScope.projectScope(project)).findAll()) {
                val element = reference.element
                val user = element.containingFile ?: continue
                if (user.originalFile == moved.originalFile) continue
                val spelled = spelledOut(reference, oldPackage)
                if (spelled != null) {
                    edits += Edit(user, spelled, newName)
                } else if (needsImportForMove(user, oldPackage, oldName) && packageOf(user) != newPackage) {
                    imports.getOrPut(user) { java.util.TreeSet() } += newName
                }
            }
        }

        // What the moved file used from the old package without saying so.
        if (oldPackage.isNotEmpty() || newPackage.isNotEmpty()) {
            val explicit = importedNames(moved)
            for (element in com.intellij.psi.SyntaxTraverser.psiTraverser(moved)) {
                for (reference in element.references) {
                    val target = reference.resolve() ?: continue
                    val top = topLevelOf(target) ?: continue
                    val home = top.containingFile ?: continue
                    if (home.originalFile == moved.originalFile || packageOf(home) != oldPackage) continue
                    val name = (top as? PsiNamedElement)?.name ?: continue
                    if (element.text != name && reference.canonicalText != name) continue   // spelled out: not implicit
                    val fq = qualified(oldPackage, name)
                    if (fq !in explicit) imports.getOrPut(moved) { java.util.TreeSet() } += fq
                }
            }
        }
    }

    /** The top-level declaration [target] is, or the class it is the constructor of; null for members. */
    private fun topLevelOf(target: PsiElement): PsiElement? {
        if (target.parent is PsiFile) return target
        val owner = target.parent
        if (owner != null && owner.parent is PsiFile && target.javaClass.simpleName.contains("Constructor", ignoreCase = true)) return owner
        // Java constructors are methods of the class.
        if (owner != null && owner.parent is PsiFile && target.javaClass.simpleName.contains("Method") &&
            (target as? PsiNamedElement)?.name == (owner as? PsiNamedElement)?.name) return owner
        return null
    }

    /**
     * The span to rewrite when the name is spelled out in full - `import a.b.C`, `a.b.C()` -
     * or null when it is written bare. Textual on purpose: the syntax trees of the two
     * languages disagree about what `a.b.C(...)` is a node of, and the text does not.
     */
    private fun spelledOut(reference: com.intellij.psi.PsiReference, oldPackage: String): TextRange? {
        if (oldPackage.isEmpty()) return null
        val element = reference.element
        val name = reference.rangeInElement.shiftRight(element.textRange.startOffset)
        val text = element.containingFile.text
        val prefix = "$oldPackage."
        if (name.startOffset < prefix.length || text.substring(name.startOffset - prefix.length, name.startOffset) != prefix) return null
        // Not the tail of a longer name: `xa.b.C` is not `a.b.C`.
        val before = name.startOffset - prefix.length - 1
        if (before >= 0 && (text[before].isJavaIdentifierPart() || text[before] == '.')) return null
        return TextRange(name.startOffset - prefix.length, name.endOffset)
    }

    /** A user that will lose the name: it is in the old package, or star-imports it, and does not import it by name. */
    private fun needsImportForMove(user: PsiFile, oldPackage: String, oldName: String): Boolean {
        val explicit = importedNames(user)
        if (oldName in explicit) return false
        return packageOf(user) == oldPackage || "$oldPackage.*" in explicit
    }

    // -------------------------------------------------------- package and imports
    private fun packageElement(file: PsiFile): PsiElement? =
        file.children.firstOrNull { it.javaClass.simpleName.contains("Package") }

    private fun importList(file: PsiFile): PsiElement? =
        file.children.firstOrNull { it.javaClass.simpleName.contains("ImportList") }

    private val packageName = Regex("""package\s+([^\s;]+)""")

    private fun packageOf(file: PsiFile): String =
        packageElement(file)?.text?.let { packageName.find(it)?.groupValues?.get(1) }.orEmpty()

    /** Names imported by [file], with `.*` for a star import. */
    private fun importedNames(file: PsiFile): Set<String> =
        importList(file)?.children.orEmpty()
            .filter { it.javaClass.simpleName.contains("Import") }
            .mapNotNull { Regex("""import\s+(?:static\s+)?([^\s;]+)""").find(it.text)?.groupValues?.get(1) }
            .toSet()

    /** The package a file in [directory] has: its path below the source root, as dots. */
    private fun packageFor(directory: Path?, file: VirtualFile): String? {
        if (directory == null) return null
        val roots = ProjectRootManager.getInstance(project).contentSourceRoots.map { Path.of(it.path) }
        val own = ProjectFileIndex.getInstance(project).getSourceRootForFile(file)?.let { Path.of(it.path) }
        val root = (listOfNotNull(own) + roots).firstOrNull { directory.startsWith(it) } ?: return null
        return root.relativize(directory).joinToString(".") { it.toString() }
    }

    private fun packageEdit(file: PsiFile, newPackage: String): Edit? {
        val element = packageElement(file)
        val text = element?.text.orEmpty()
        val match = packageName.find(text)
        if (element == null || match == null) {
            if (newPackage.isEmpty()) return null
            val semicolon = if (file.language.id == "JAVA") ";" else ""
            return Edit(file, TextRange(0, 0), "package $newPackage$semicolon\n\n")
        }
        val group = match.groups[1]!!.range
        val start = element.textRange.startOffset
        return Edit(file, TextRange(start + group.first, start + group.last + 1), newPackage)
    }

    private fun importInsertion(file: PsiFile, names: Set<String>, edits: MutableList<Edit>) {
        val semicolon = if (file.language.id == "JAVA") ";" else ""
        val lines = names.joinToString("\n") { "import $it$semicolon" }
        val list = importList(file)
        val last = list?.children?.lastOrNull { it.javaClass.simpleName.contains("Import") }
        if (last != null) {
            edits += Edit(file, TextRange(last.textRange.endOffset, last.textRange.endOffset), "\n$lines")
            return
        }
        val pkg = packageElement(file)?.takeIf { it.text.isNotBlank() }
        if (pkg != null) {
            // Merge with the package edit when it ends where this goes (Kotlin has no semicolon).
            val end = pkg.textRange.endOffset
            val clash = edits.lastOrNull { it.file == file && it.range.endOffset == end && it.range.startOffset < end }
            if (clash != null) {
                edits.remove(clash)
                edits += Edit(file, clash.range, clash.text + "\n\n$lines")
            } else {
                edits += Edit(file, TextRange(end, end), "\n\n$lines")
            }
        } else {
            edits += Edit(file, TextRange(0, 0), "$lines\n\n")
        }
    }
}
