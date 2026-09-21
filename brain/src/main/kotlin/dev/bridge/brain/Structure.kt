package dev.bridge.brain

import com.intellij.codeInsight.editorActions.SelectWordUtil
import com.intellij.ide.structureView.StructureViewTreeElement
import com.intellij.ide.structureView.TreeBasedStructureViewBuilder
import com.intellij.ide.util.treeView.smartTree.TreeElement
import com.intellij.lang.LanguageStructureViewBuilder
import com.intellij.lang.folding.LanguageFolding
import com.intellij.navigation.ChooseByNameContributor
import com.intellij.navigation.NavigationItem
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.util.TextRange
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiNameIdentifierOwner
import com.intellij.psi.PsiNamedElement
import com.intellij.psi.codeStyle.NameUtil
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Symbols, folding and selection (FEATURES.md §3-4), all read-only and all
 * answered from IntelliJ's own models: the structure view, the folding
 * builders, the word-selection handlers, and Go to Symbol.
 */
class StructureFeatures(private val project: Project, private val locations: Locations) {

    // ----------------------------------------------------------- document symbols
    /** Hierarchical `DocumentSymbol[]` from the file's structure view. */
    fun documentSymbols(editor: Editor, file: PsiFile): JsonElement {
        val builder = LanguageStructureViewBuilder.getInstance().getStructureViewBuilder(file)
            as? TreeBasedStructureViewBuilder ?: return JsonArray(emptyList())
        val model = builder.createStructureViewModel(editor)
        try {
            return JsonArray(model.root.children.mapNotNull { symbol(it, editor.document) })
        } finally {
            Disposer.dispose(model)
        }
    }

    private fun symbol(node: TreeElement, doc: Document): JsonObject? {
        val tree = node as? StructureViewTreeElement ?: return null
        val element = tree.value as? PsiElement ?: return null
        val range = element.textRange ?: return null
        val name = (element as? PsiNamedElement)?.name
            ?: tree.presentation.presentableText ?: return null
        val nameRange = (element as? PsiNameIdentifierOwner)?.nameIdentifier?.textRange
            ?.takeIf { range.contains(it) } ?: range
        val presented = tree.presentation.presentableText
        val children = tree.children.mapNotNull { symbol(it, doc) }
        return buildJsonObject {
            put("name", name)
            if (presented != null && presented != name) put("detail", presented.removePrefix(name))
            put("kind", kind(element))
            put("range", range(doc, range))
            put("selectionRange", range(doc, nameRange))
            if (children.isNotEmpty()) put("children", JsonArray(children))
        }
    }

    // ---------------------------------------------------------- workspace symbols
    /** `SymbolInformation[]` from IntelliJ's Go to Class / Go to Symbol. */
    fun workspaceSymbols(query: String): JsonElement {
        if (query.isBlank()) return JsonArray(emptyList())
        val matcher = NameUtil.buildMatcher("*$query").build()
        val contributors = ChooseByNameContributor.CLASS_EP_NAME.extensionList +
            ChooseByNameContributor.SYMBOL_EP_NAME.extensionList

        val names = LinkedHashSet<Pair<ChooseByNameContributor, String>>()
        for (c in contributors) {
            for (n in c.getNames(project, false)) if (matcher.matches(n)) names += c to n
        }
        // Exact, then prefix, then the rest: the picker shows the first screenful.
        val ranked = names.sortedWith(compareBy({ rank(it.second, query) }, { it.second.length }, { it.second }))

        val out = ArrayList<JsonObject>()
        val seen = HashSet<String>()
        for ((contributor, name) in ranked) {
            if (out.size >= MAX_SYMBOLS) break
            for (item in contributor.getItemsByName(name, query, project, false)) {
                val element = elementOf(item) ?: continue
                val location = locations.of(element) ?: continue
                if (!seen.add(location.toString())) continue
                out += buildJsonObject {
                    put("name", (element as? PsiNamedElement)?.name ?: name)
                    put("kind", kind(element))
                    put("location", location)
                    item.presentation?.locationString?.trim('(', ')', ' ')?.takeIf { it.isNotBlank() }
                        ?.let { put("containerName", it) }
                }
            }
        }
        return JsonArray(out)
    }

    private fun elementOf(item: NavigationItem): PsiElement? =
        (item as? PsiElement) ?: (item as? com.intellij.navigation.PsiElementNavigationItem)?.targetElement

    private fun rank(name: String, query: String) = when {
        name.equals(query, ignoreCase = true) -> 0
        name.startsWith(query, ignoreCase = true) -> 1
        else -> 2
    }

    // -------------------------------------------------------------------- folding
    /** `FoldingRange[]` from IntelliJ's folding builders. */
    fun foldingRanges(file: PsiFile, doc: Document): JsonElement {
        val seen = HashSet<Pair<Int, Int>>()
        val out = ArrayList<JsonObject>()
        for (builder in LanguageFolding.INSTANCE.allForLanguage(file.language)) {
            for (d in LanguageFolding.buildFoldingDescriptors(builder, file, doc, false)) {
                val r = d.range
                if (r.isEmpty || r.endOffset > doc.textLength) continue
                val startLine = doc.getLineNumber(r.startOffset)
                var endLine = doc.getLineNumber(r.endOffset)
                // LSP hides through the end line. Keep a lone closing bracket
                // visible, as every other server's folds do.
                val last = doc.text.substring(doc.getLineStartOffset(endLine), doc.getLineEndOffset(endLine)).trim()
                if (endLine > startLine && last in setOf("}", "})", "};", ")", "]")) endLine -= 1
                if (endLine <= startLine || !seen.add(startLine to endLine)) continue
                val type = d.element.elementType.toString()
                val kind = when {
                    type.contains("COMMENT", ignoreCase = true) || type.contains("DOC", ignoreCase = true) -> "comment"
                    type.contains("IMPORT", ignoreCase = true) -> "imports"
                    else -> "region"
                }
                out += buildJsonObject {
                    put("startLine", startLine)
                    put("endLine", endLine)
                    put("kind", kind)
                    d.placeholderText?.takeIf { it.isNotBlank() }?.let { put("collapsedText", it) }
                }
            }
        }
        return JsonArray(out.sortedBy { it["startLine"].toString().toInt() })
    }

    // ------------------------------------------------------------------ selection
    /** For each position, the chain of ever-larger ranges IntelliJ's Extend Selection walks. */
    fun selectionRanges(editor: Editor, file: PsiFile, positions: List<JsonObject>): JsonElement =
        JsonArray(positions.map { selectionChain(editor, file, MirrorSet.offset(editor.document, it)) })

    private fun selectionChain(editor: Editor, file: PsiFile, offset: Int): JsonElement {
        val doc = editor.document
        val leaf = file.findElementAt(offset) ?: file.findElementAt((offset - 1).coerceAtLeast(0))
        val found = ArrayList<TextRange>()
        if (leaf != null) {
            SelectWordUtil.processRanges(leaf, doc.charsSequence, offset, editor) { found += it; true }
            // IntelliJ's handlers give the word and a few coarse ranges; the ancestors
            // are what fill the gap (call, qualified expression, statement, block).
            var e: PsiElement? = leaf
            while (e != null && e !is PsiFile) {
                if (e !is com.intellij.psi.PsiWhiteSpace) found += e.textRange
                e = e.parent
            }
        }
        // Innermost first, each strictly containing the last; the file caps the chain.
        val chain = ArrayList<TextRange>()
        for (r in found.filter { it.containsOffset(offset) || it.endOffset == offset }
            .distinct().sortedBy { it.length }) {
            if (chain.isEmpty() || (r.contains(chain.last()) && r != chain.last())) chain += r
        }
        val whole = TextRange(0, doc.textLength)
        if (chain.isEmpty() || chain.last() != whole) chain += whole
        var node: JsonObject? = null
        for (r in chain.asReversed()) {
            val parent = node
            node = buildJsonObject {
                put("range", range(doc, r))
                if (parent != null) put("parent", parent)
            }
        }
        return node ?: JsonNull
    }

    // -------------------------------------------------------------------- helpers
    private fun range(doc: Document, r: TextRange) = buildJsonObject {
        put("start", Locations.position(doc, r.startOffset))
        put("end", Locations.position(doc, r.endOffset))
    }

    /**
     * LSP SymbolKind from the PSI. Language-agnostic by name: Java's and Kotlin's
     * PSI classes are not both on this plugin's classpath, and the structure of
     * their names is stable.
     */
    private fun kind(e: PsiElement): Int {
        val n = e::class.java.simpleName
        val parentName = e.parent?.let { it::class.java.simpleName } ?: ""
        val head = e.text.take(300).substringBefore('{').substringBefore('(')
        return when {
            n.contains("Constructor") -> 9
            n.contains("Method") || n == "KtNamedFunction" -> when {
                n.contains("Method") && (e as? PsiNamedElement)?.name ==
                    (e.parent as? PsiNamedElement)?.name -> 9
                parentName.contains("File") -> 12
                else -> 6
            }
            n == "KtEnumEntry" || n.contains("EnumConstant") -> 22
            n == "KtProperty" || n == "KtParameter" -> 7
            n.contains("Field") -> 8
            n == "KtTypeAlias" -> 26
            n.contains("Class") || n == "KtObjectDeclaration" -> when {
                Regex("\\binterface\\b").containsMatchIn(head) -> 11
                Regex("\\benum\\b").containsMatchIn(head) -> 10
                else -> 5
            }
            else -> 13
        }
    }

    companion object {
        const val MAX_SYMBOLS = 100
    }
}
