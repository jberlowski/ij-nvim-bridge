package dev.bridge.brain

import com.intellij.openapi.editor.EditorFactory
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiFile
import com.intellij.psi.codeStyle.CodeStyleManager
import com.intellij.psi.util.PsiTreeUtil
import org.jetbrains.kotlin.analysis.api.permissions.KaAllowAnalysisOnEdt
import org.jetbrains.kotlin.analysis.api.permissions.allowAnalysisOnEdt
import org.jetbrains.kotlin.idea.core.overrideImplement.KtImplementMembersHandler
import org.jetbrains.kotlin.lexer.KtTokens
import org.jetbrains.kotlin.psi.KtClass
import org.jetbrains.kotlin.psi.KtFile
import org.jetbrains.kotlin.psi.KtNamedFunction
import org.jetbrains.kotlin.psi.KtParameter
import org.jetbrains.kotlin.psi.KtProperty
import org.jetbrains.kotlin.psi.KtPsiFactory

/**
 * Generate for Kotlin: `toString`, and `equals` with `hashCode`, from the class's properties, in the
 * text of IntelliJ's default templates. Kotlin's own Generate actions run their analysis in a modal
 * window, which cannot be waited for here, so the members are built from the syntax tree: the
 * properties, and their declared types (a property whose type is inferred is compared as an object).
 *
 * Not offered for a data class, which has these already. Implementing missing members needs the
 * analysis API and is not here yet.
 */
class KotlinGenerator : Generator {

    override fun handles(file: PsiFile) = file is KtFile

    /** Implementing members analyses and writes by itself, and analysis may not run inside a write action. */
    override fun needsWriteAction(key: String) = key != "implement"

    private fun classAt(file: PsiFile, offset: Int): KtClass? {
        val at = file.findElementAt(offset) ?: file.findElementAt((offset - 1).coerceAtLeast(0)) ?: return null
        return PsiTreeUtil.getParentOfType(at, KtClass::class.java, false)
            ?.takeIf { it.name != null && !it.isInterface() && !it.isAnnotation() && !it.isData() }
    }

    /** A property the class stores: constructor `val`/`var`, and body properties with a backing field. */
    private class Prop(val name: String, val type: String?)

    private fun propertiesOf(kt: KtClass): List<Prop> {
        val fromConstructor = kt.primaryConstructorParameters.filter(KtParameter::hasValOrVar)
            .map { Prop(it.name ?: return@map null, it.typeReference?.text) }
        val fromBody = kt.declarations.filterIsInstance<KtProperty>()
            .filter { !it.isLocal && !it.hasDelegate() && !(it.getter != null && it.initializer == null) }
            .map { Prop(it.name ?: return@map null, it.typeReference?.text) }
        return (fromConstructor + fromBody).filterNotNull()
    }

    /** A class that is not abstract must implement what it inherits as abstract. */
    private fun missing(kt: KtClass): Int =
        if (kt.hasModifier(KtTokens.ABSTRACT_KEYWORD) || kt.hasModifier(KtTokens.SEALED_KEYWORD) || kt.isEnum()) 0
        else runCatching { KtImplementMembersHandler().collectMembersToGenerate(kt).size }.getOrDefault(0)

    override fun offers(file: PsiFile, offset: Int): List<Generator.Offer> {
        val kt = classAt(file, offset) ?: return emptyList()
        val out = ArrayList<Generator.Offer>()
        val unimplemented = missing(kt)
        if (unimplemented > 0) {
            out += Generator.Offer("implement", "Implement $unimplemented missing member${if (unimplemented == 1) "" else "s"}", "source.generate.implementMembers")
        }
        if (propertiesOf(kt).isEmpty()) return out
        val functions = kt.declarations.filterIsInstance<KtNamedFunction>()
        if (functions.none { it.name == "toString" && it.valueParameters.isEmpty() }) {
            out += Generator.Offer("toString", "Generate toString()", "source.generate.toString")
        }
        if (functions.none { it.name == "equals" || it.name == "hashCode" }) {
            out += Generator.Offer("equalsHashCode", "Generate equals() and hashCode()", "source.generate.equalsHashCode")
        }
        return out
    }

    override fun generate(copy: PsiFile, offset: Int, key: String) {
        val kt = classAt(copy, offset) ?: throw IllegalStateException("no class at the caret")
        if (key == "implement") return implement(copy, kt)
        val props = propertiesOf(kt)
        val factory = KtPsiFactory(kt.project)
        val functions = when (key) {
            "toString" -> listOf(toStringText(kt, props))
            "equalsHashCode" -> listOf(equalsText(kt, props), hashCodeText(props))
            else -> throw IllegalArgumentException("unknown generator: $key")
        }
        for (text in functions) {
            val added = kt.addDeclaration(factory.createFunction(text))
            CodeStyleManager.getInstance(kt.project).reformat(added)
        }
    }

    /**
     * IntelliJ's own Implement Members, without its chooser: all the members it lists. It wants an editor
     * to place them by the caret, so the copy gets a throwaway one.
     */
    private fun implement(copy: PsiFile, kt: KtClass) {
        val project = copy.project
        val document = PsiDocumentManager.getInstance(project).getDocument(copy)
            ?: copy.viewProvider.document
            ?: throw IllegalStateException("the copy has no document")
        val editor = EditorFactory.getInstance().createEditor(document, project)
        try {
            editor.caretModel.moveToOffset(kt.textOffset)
            val handler = KtImplementMembersHandler()
            // K2 refuses analysis on the EDT, and writing the copy must be there: it is brief, and the
            // IDE's own handlers make the same allowances when they generate in a write action.
            @OptIn(KaAllowAnalysisOnEdt::class)
            allowAnalysisOnEdt {
                try {
                    handler.generateMembers(editor, kt, handler.collectMembersToGenerate(kt), false)
                } catch (e: IllegalArgumentException) {
                    // After inserting, it opens the file to put the caret in the new member, and a copy
                    // has no file to open. The members are in by then; only that step fails.
                    if (e.message?.contains("OpenFileDescriptor") != true) throw e
                }
            }
            PsiDocumentManager.getInstance(project).doPostponedOperationsAndUnblockDocument(document)
            PsiDocumentManager.getInstance(project).commitDocument(document)
        } finally {
            EditorFactory.getInstance().releaseEditor(editor)
        }
    }

    // ---------------------------------------------------- the default templates' text
    private fun toStringText(kt: KtClass, props: List<Prop>) =
        "override fun toString(): String {\n    return \"${kt.name}(${props.joinToString(", ") { "${it.name}=\$${it.name}" }})\"\n}"

    private fun isArray(type: String?) = type != null && (type.startsWith("Array<") || type.endsWith("Array"))

    private fun equalsText(kt: KtClass, props: List<Prop>): String {
        val checks = props.joinToString("\n") { p ->
            if (isArray(p.type)) "    if (!${p.name}.contentEquals(other.${p.name})) return false"
            else "    if (${p.name} != other.${p.name}) return false"
        }
        return "override fun equals(other: Any?): Boolean {\n" +
            "    if (this === other) return true\n" +
            "    if (javaClass != other?.javaClass) return false\n\n" +
            "    other as ${kt.name}\n\n$checks\n\n    return true\n}"
    }

    private fun hashCodeText(props: List<Prop>): String {
        fun hash(p: Prop): String {
            val nullable = p.type?.endsWith("?") == true
            return when {
                isArray(p.type) -> "${p.name}${if (nullable) "?" else ""}.contentHashCode()${if (nullable) " ?: 0" else ""}"
                p.type == "Int" -> p.name
                else -> "${p.name}${if (nullable) "?" else ""}.hashCode()${if (nullable) " ?: 0" else ""}"
            }
        }
        val first = hash(props.first())
        // `31 * result + x?.hashCode() ?: 0` would parse as `(31 * result + x?.hashCode()) ?: 0`.
        val rest = props.drop(1).joinToString("\n") { p -> hash(p).let { h -> "    result = 31 * result + ${if ("?:" in h) "($h)" else h}" } }
        return "override fun hashCode(): Int {\n    var result = $first\n${if (rest.isEmpty()) "" else "$rest\n"}    return result\n}"
    }
}
