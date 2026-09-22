package dev.bridge.brain

import com.intellij.codeInsight.generation.GenerateConstructorHandler
import com.intellij.codeInsight.generation.GenerateMembersUtil
import com.intellij.codeInsight.generation.OverrideImplementExploreUtil
import com.intellij.codeInsight.generation.OverrideImplementUtil
import com.intellij.codeInsight.generation.PsiGenerationInfo
import com.intellij.openapi.project.Project
import com.intellij.psi.JavaPsiFacade
import com.intellij.psi.PsiArrayType
import com.intellij.psi.PsiClass
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiEnumConstant
import com.intellij.psi.PsiField
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiJavaFile
import com.intellij.psi.PsiMember
import com.intellij.psi.PsiMethod
import com.intellij.psi.PsiModifier
import com.intellij.psi.PsiPrimitiveType
import com.intellij.psi.PsiTypes
import com.intellij.psi.codeStyle.CodeStyleManager
import com.intellij.psi.codeStyle.JavaCodeStyleManager
import com.intellij.psi.util.PropertyUtilBase
import com.intellij.psi.util.PsiTreeUtil

/**
 * Generate for Java, with IntelliJ's own helpers for accessors, constructors and implementing
 * methods (the same the Generate menu uses, minus its dialogs), and its default templates' text for
 * `toString`, `equals` and `hashCode`. Everything is built on the *copy* (see [Generator]).
 *
 * Choices the dialogs would ask about are made the way the dialogs start: **all** the fields.
 */
class JavaGenerator : Generator {

    override fun handles(file: PsiFile) = file is PsiJavaFile

    private fun classAt(file: PsiFile, offset: Int): PsiClass? {
        val at = file.findElementAt(offset) ?: file.findElementAt((offset - 1).coerceAtLeast(0)) ?: return null
        return PsiTreeUtil.getParentOfType(at, PsiClass::class.java, false)?.takeIf { it.name != null && !it.isInterface && !it.isAnnotationType }
    }

    /** The instance fields a constructor or `equals` is about: not static, not constants, not enum constants. */
    private fun fieldsOf(aClass: PsiClass): List<PsiField> = aClass.fields.filter {
        it !is PsiEnumConstant && !it.hasModifierProperty(PsiModifier.STATIC) &&
            !(it.hasModifierProperty(PsiModifier.FINAL) && it.initializer != null)
    }

    override fun offers(file: PsiFile, offset: Int): List<Generator.Offer> {
        val aClass = classAt(file, offset) ?: return emptyList()
        val fields = fieldsOf(aClass)
        val out = ArrayList<Generator.Offer>()

        val missing = missingImplementations(aClass)
        if (missing > 0 && !aClass.hasModifierProperty(PsiModifier.ABSTRACT)) {
            out += Generator.Offer("implement", "Implement $missing missing method${if (missing == 1) "" else "s"}", "source.generate.implementMembers")
        }
        if (fields.isNotEmpty()) {
            if (aClass.constructors.none { it.parameterList.parametersCount == fields.size }) {
                out += Generator.Offer("constructor", "Generate constructor", "source.generate.constructor")
            }
            val noGetter = fields.filter { getter(aClass, it) == null }
            val noSetter = fields.filter { !it.hasModifierProperty(PsiModifier.FINAL) && setter(aClass, it) == null }
            if (noGetter.isNotEmpty()) out += Generator.Offer("getters", "Generate getters", "source.generate.getters")
            if (noSetter.isNotEmpty()) out += Generator.Offer("setters", "Generate setters", "source.generate.setters")
            if (noGetter.isNotEmpty() && noSetter.isNotEmpty()) {
                out += Generator.Offer("accessors", "Generate getters and setters", "source.generate.accessors")
            }
            if (aClass.findMethodsByName("toString", false).none { it.parameterList.isEmpty }) {
                out += Generator.Offer("toString", "Generate toString()", "source.generate.toString")
            }
            val hasEquals = aClass.findMethodsByName("equals", false).any { it.parameterList.parametersCount == 1 }
            val hasHash = aClass.findMethodsByName("hashCode", false).any { it.parameterList.isEmpty }
            if (!hasEquals && !hasHash) out += Generator.Offer("equalsHashCode", "Generate equals() and hashCode()", "source.generate.equalsHashCode")
        }
        return out
    }

    private fun getter(aClass: PsiClass, field: PsiField) =
        PropertyUtilBase.findPropertyGetter(aClass, field.name, false, false)

    private fun setter(aClass: PsiClass, field: PsiField) =
        PropertyUtilBase.findPropertySetter(aClass, field.name, false, false)

    private fun missingImplementations(aClass: PsiClass): Int =
        runCatching { OverrideImplementExploreUtil.getMethodsToOverrideImplement(aClass, true).size }.getOrDefault(0)

    // --------------------------------------------------------------- generating
    override fun generate(copy: PsiFile, offset: Int, key: String) {
        val aClass = classAt(copy, offset) ?: throw IllegalStateException("no class at the caret")
        val project = aClass.project
        val fields = fieldsOf(aClass)
        when (key) {
            "constructor" -> {
                val constructor = GenerateConstructorHandler.generateConstructorPrototype(aClass, null, false, fields.toTypedArray())
                val after: PsiElement? = fields.lastOrNull()
                val added = if (after != null) aClass.addAfter(constructor, after) else aClass.add(constructor)
                tidy(project, added)
            }
            "getters" -> insertAll(aClass, fields.filter { getter(aClass, it) == null }.map { GenerateMembersUtil.generateGetterPrototype(it) })
            "setters" -> insertAll(aClass, fields.filter { !it.hasModifierProperty(PsiModifier.FINAL) && setter(aClass, it) == null }
                .map { GenerateMembersUtil.generateSetterPrototype(it) })
            "accessors" -> insertAll(aClass, fields.flatMap { field ->
                listOfNotNull(
                    if (getter(aClass, field) == null) GenerateMembersUtil.generateGetterPrototype(field) else null,
                    if (!field.hasModifierProperty(PsiModifier.FINAL) && setter(aClass, field) == null) GenerateMembersUtil.generateSetterPrototype(field) else null,
                )
            })
            "toString" -> insertAll(aClass, listOf(method(aClass, toStringText(aClass, fields))))
            "equalsHashCode" -> insertAll(aClass, listOf(method(aClass, equalsText(aClass, fields)), method(aClass, hashCodeText(fields))))
            "implement" -> {
                val candidates = OverrideImplementExploreUtil.getMethodsToOverrideImplement(aClass, true)
                val prototypes = OverrideImplementUtil.overrideOrImplementMethodCandidates(aClass, candidates, false)
                insertAll(aClass, prototypes)
            }
            else -> throw IllegalArgumentException("unknown generator: $key")
        }
    }

    private fun method(aClass: PsiClass, text: String): PsiMethod =
        JavaPsiFacade.getElementFactory(aClass.project).createMethodFromText(text, aClass)

    /** A public instance method worth its own test: not a constructor, not inherited (`Object`'s), not a synthetic accessor. */
    private fun testableMethods(aClass: PsiClass): List<PsiMethod> = aClass.methods.filter {
        it.hasModifierProperty(PsiModifier.PUBLIC) && !it.isConstructor && !it.hasModifierProperty(PsiModifier.STATIC)
    }

    override fun testSkeleton(file: PsiFile, offset: Int, testClassName: String, testPackage: String): String? {
        val aClass = classAt(file, offset) ?: return null
        val body = testableMethods(aClass).joinToString("\n\n") { m ->
            val name = m.name.replaceFirstChar { it.uppercase() }
            "    @Test\n    void test$name() {\n    }"
        }
        val pkg = if (testPackage.isEmpty()) "" else "package $testPackage;\n\n"
        return "$pkg" + "import org.junit.jupiter.api.Test;\n\nclass $testClassName {\n\n$body\n}\n"
    }

    /** At the end of the class, as the Generate menu does by default, then formatted and its names imported. */
    private fun insertAll(aClass: PsiClass, methods: List<PsiMethod>) {
        if (methods.isEmpty()) return
        val infos = methods.map { PsiGenerationInfo(it) }
        val inserted = GenerateMembersUtil.insertMembersBeforeAnchor(aClass, aClass.rBrace, infos)
        inserted.forEach { info -> info.psiMember?.let { tidy(aClass.project, it) } }
    }

    private fun tidy(project: Project, element: PsiElement) {
        JavaCodeStyleManager.getInstance(project).shortenClassReferences(element)
        CodeStyleManager.getInstance(project).reformat(element)
    }

    // ---------------------------------------------------- the default templates' text
    private fun toStringText(aClass: PsiClass, fields: List<PsiField>): String {
        val parts = fields.mapIndexed { i, f ->
            val prefix = (if (i == 0) "" else ", ") + f.name + "="
            if (f.type == PsiTypes.charType() || f.type.equalsToText("java.lang.String")) "\"$prefix'\" + ${f.name} + '\\''"
            else "\"$prefix\" + ${f.name}"
        }
        return "@Override public String toString() { return \"${aClass.name}{\" + " + (parts + "'}'").joinToString(" + ") + "; }"
    }

    private fun equalsText(aClass: PsiClass, fields: List<PsiField>): String {
        val type = aClass.name + if (aClass.typeParameters.isEmpty()) "" else "<" + aClass.typeParameters.joinToString(", ") { "?" } + ">"
        val same = fields.joinToString(" && ") { f ->
            val other = "that.${f.name}"
            when (val t = f.type) {
                PsiTypes.doubleType(), PsiTypes.floatType() -> "${t.presentableText.replaceFirstChar { it.uppercase() }}.compare(${f.name}, $other) == 0"
                is PsiPrimitiveType -> "${f.name} == $other"
                is PsiArrayType -> "java.util.Arrays.equals(${f.name}, $other)"
                else -> "java.util.Objects.equals(${f.name}, $other)"
            }
        }
        return "@Override public boolean equals(Object o) { if (this == o) return true; " +
            "if (o == null || getClass() != o.getClass()) return false; $type that = ($type) o; return $same; }"
    }

    private fun hashCodeText(fields: List<PsiField>): String =
        "@Override public int hashCode() { return java.util.Objects.hash(${fields.joinToString(", ") { it.name }}); }"
}
