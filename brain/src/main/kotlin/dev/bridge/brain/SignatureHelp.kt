package dev.bridge.brain

import com.intellij.lang.parameterInfo.CreateParameterInfoContext
import com.intellij.lang.parameterInfo.LanguageParameterInfo
import com.intellij.lang.parameterInfo.ParameterInfoHandler
import com.intellij.lang.parameterInfo.ParameterInfoUIContext
import com.intellij.lang.parameterInfo.UpdateParameterInfoContext
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.UserDataHolderBase
import com.intellij.openapi.util.UserDataHolderEx
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.awt.Color

/**
 * `textDocument/signatureHelp`, from IntelliJ's own parameter-info handlers.
 *
 * Those handlers are written for a popup: they are driven through "context"
 * interfaces and report what to draw by calling back into a UI context. This
 * plays the part of the popup and keeps what it is told - the signature
 * label, each parameter, which one is current - so the answer is exactly what
 * IntelliJ would have shown, overloads included.
 */
class SignatureHelp {

    fun signatureHelp(project: Project, editor: Editor, file: PsiFile, offset: Int): JsonElement {
        for (raw in LanguageParameterInfo.INSTANCE.allForLanguage(file.language)) {
            @Suppress("UNCHECKED_CAST")
            val handler = raw as ParameterInfoHandler<PsiElement, Any>

            val create = Create(project, file, editor, offset)
            val owner = handler.findElementForParameterInfo(create) ?: continue
            handler.showParameterInfo(owner, create)
            val items = create.items?.takeIf { it.isNotEmpty() } ?: continue

            val update = Update(project, file, editor, offset, items, owner)
            val element = handler.findElementForUpdatingParameterInfo(update) ?: owner
            update.parameterOwner = element
            handler.updateParameterInfo(element, update)

            val signatures = items.map { item ->
                val ui = Ui(update.current, element, items.size == 1)
                handler.updateUI(item, ui)
                ui.toSignature(handler, item, element)
            }
            val currentParameter = update.current.coerceAtLeast(0)
            val active = items.indexOfFirst { it === update.highlighted }.takeIf { it >= 0 }
                // Not every handler says which overload matches; the first one with
                // enough parameters for the one being typed is the natural choice.
                ?: signatures.indexOfFirst {
                    ((it["parameters"] as? JsonArray)?.size ?: 0) > currentParameter
                }.takeIf { it >= 0 } ?: 0
            return buildJsonObject {
                put("signatures", JsonArray(signatures))
                put("activeSignature", active)
                put("activeParameter", currentParameter)
            }
        }
        return JsonNull
    }

    // ---------------------------------------------------------------- contexts
    private class Create(private val project: Project, private val file: PsiFile,
                         private val editor: Editor, private val offset: Int) : CreateParameterInfoContext {
        var items: Array<Any>? = null
        private var highlightedElement: PsiElement? = null
        override fun getProject() = project
        override fun getFile() = file
        override fun getOffset() = offset
        override fun getEditor() = editor
        override fun getItemsToShow(): Array<Any>? = items
        override fun setItemsToShow(items: Array<Any>?) { this.items = items }
        override fun showHint(element: PsiElement?, offset: Int, handler: ParameterInfoHandler<*, *>?) {}
        override fun getParameterListStart() = offset
        override fun getHighlightedElement() = highlightedElement
        override fun setHighlightedElement(element: PsiElement?) { highlightedElement = element }
    }

    private class Update(private val project: Project, private val file: PsiFile, private val editor: Editor,
                         private val offset: Int, private val items: Array<Any>,
                         private var owner: PsiElement?) : UpdateParameterInfoContext {
        var current = -1
        var highlighted: Any? = null
        private val enabled = BooleanArray(items.size) { true }
        private var preserved = false
        private val custom = UserDataHolderBase()
        override fun getProject() = project
        override fun getFile() = file
        override fun getOffset() = offset
        override fun getEditor() = editor
        override fun removeHint() {}
        override fun setParameterOwner(o: PsiElement?) { owner = o }
        override fun getParameterOwner(): PsiElement? = owner
        override fun setHighlightedParameter(p: Any?) { highlighted = p }
        override fun getHighlightedParameter(): Any? = highlighted
        override fun setCurrentParameter(index: Int) { current = index }
        override fun isUIComponentEnabled(index: Int) = enabled.getOrElse(index) { true }
        override fun setUIComponentEnabled(index: Int, value: Boolean) { if (index in enabled.indices) enabled[index] = value }
        override fun getParameterListStart() = offset
        override fun getObjectsToView(): Array<Any> = items
        override fun isPreservedOnHintHidden() = preserved
        override fun setPreservedOnHintHidden(value: Boolean) { preserved = value }
        override fun isInnermostContext() = true
        override fun isSingleParameterInfo() = false
        override fun getCustomContext(): UserDataHolderEx = custom
    }

    /** The popup. Records what it is asked to draw. */
    private class Ui(private val currentIndex: Int, private val owner: PsiElement?,
                     private val single: Boolean) : ParameterInfoUIContext {
        var text: String? = null
        var highlight: Pair<Int, Int>? = null
        var htmlParams: List<ParameterInfoUIContext.ParameterHtmlPresentation>? = null
        private var enabled = true

        override fun setupUIComponentPresentation(text: String?, start: Int, end: Int, disabled: Boolean,
                                                  strikeout: Boolean, disabledBefore: Boolean,
                                                  background: Color?): String? {
            this.text = text
            if (start >= 0 && end >= start) highlight = start to end
            return text
        }

        override fun setupRawUIComponentPresentation(htmlText: String?) { text = htmlText }

        override fun setupSignatureHtmlPresentation(
            parameters: MutableList<ParameterInfoUIContext.ParameterHtmlPresentation>,
            currentParameterIndex: Int, description: String, disabled: Boolean,
        ) {
            htmlParams = parameters.toList()
        }

        override fun isUIComponentEnabled() = enabled
        override fun setUIComponentEnabled(value: Boolean) { enabled = value }
        override fun getCurrentParameterIndex() = currentIndex
        override fun getParameterOwner(): PsiElement? = owner
        override fun isSingleOverload() = single
        override fun isSingleParameterInfo() = false
        override fun getDefaultParameterColor(): Color = Color.BLACK

        /** One LSP SignatureInformation. */
        fun toSignature(handler: ParameterInfoHandler<PsiElement, Any>, item: Any, element: PsiElement): JsonObject {
            val structured = htmlParams
            val name = callee(element)
            if (structured != null) {
                // Newer handlers hand over the parameters as a list.
                val parts = structured.map {
                    HtmlToMarkdown.convert(it.nameAndType()) + (it.defaultValue()?.let { d -> " = " + HtmlToMarkdown.convert(d) } ?: "")
                }
                val label = "$name(${parts.joinToString(", ")})"
                var from = name.length + 1
                val infos = parts.map { p ->
                    val start = from
                    from += p.length + 2
                    buildJsonObject { put("label", JsonArray(listOf(kotlinx.serialization.json.JsonPrimitive(start),
                        kotlinx.serialization.json.JsonPrimitive(start + p.length)))) }
                }
                return buildJsonObject { put("label", label); put("parameters", JsonArray(infos)) }
            }
            // Older handlers draw one string and highlight the current parameter in it.
            var label = HtmlToMarkdown.convert(text ?: "")
            // Kotlin's handler draws just the parameter list; LSP wants "name(...)".
            if (!label.contains('(')) label = "$name($label)"
            return buildJsonObject {
                put("label", label)
                put("parameters", JsonArray(splitParameters(label)))
            }
        }

        /** The name being called: the identifier before the first '(' of the owner or its parents. */
        private fun callee(element: PsiElement): String {
            for (e in generateSequence(element) { it.parent }.take(5)) {
                val text = e.text ?: continue
                val open = text.indexOf('(')
                if (open <= 0) continue
                val name = text.substring(0, open).trim().substringAfterLast('.').substringAfterLast(' ')
                if (Regex("[A-Za-z_][A-Za-z0-9_]*").matches(name)) return name
            }
            return ""
        }

        /** Parameters of "name(a: A, b: B)" as [start, end) offsets into the label. */
        private fun splitParameters(label: String): List<JsonObject> {
            val open = label.indexOf('(')
            val close = label.lastIndexOf(')')
            if (open < 0 || close <= open) return emptyList()
            val out = ArrayList<JsonObject>()
            var depth = 0
            var start = open + 1
            for (i in open + 1..close) {
                val c = label[i]
                if (c == '<' || c == '(' || c == '[') depth++
                if (c == '>' || c == ']' || (c == ')' && i != close)) depth--
                if ((c == ',' && depth == 0) || i == close) {
                    val raw = label.substring(start, i)
                    if (raw.isNotBlank()) {
                        val s = start + (raw.length - raw.trimStart().length)
                        out += buildJsonObject {
                            put("label", JsonArray(listOf(kotlinx.serialization.json.JsonPrimitive(s),
                                kotlinx.serialization.json.JsonPrimitive(s + raw.trim().length))))
                        }
                    }
                    start = i + 1
                }
            }
            return out
        }
    }
}
