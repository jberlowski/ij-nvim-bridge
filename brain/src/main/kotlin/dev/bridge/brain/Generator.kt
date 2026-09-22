package dev.bridge.brain

import com.intellij.openapi.extensions.ExtensionPointName
import com.intellij.psi.PsiFile

/**
 * Generating code (FEATURES.md §5): what IntelliJ's Generate menu does, as code actions.
 *
 * The menu's own actions are dialogs (choose the members) that change the file when they end, so
 * they cannot answer with edits. A [Generator] does the same work programmatically, for one
 * language, on a *copy* of the file, and its result is diffed into edits like every other edit
 * the Brain computes (D2). One is registered for each language whose plugin is present (an
 * optional dependency, `brain-java.xml` and `brain-kotlin.xml`), so the Brain loads without either.
 */
interface Generator {

    /** One thing that can be generated here. [kind] is an LSP code action kind under `source.generate`. */
    class Offer(val key: String, val title: String, val kind: String)

    /** Whether this generator is for [file]'s language. */
    fun handles(file: PsiFile): Boolean

    /** What can be generated for the class at [offset]. Read action. */
    fun offers(file: PsiFile, offset: Int): List<Offer>

    /**
     * Whether [generate] for [key] should be called inside a write action. It is by default. A generator
     * that does its own analysis and its own writing (Kotlin's, which the analysis API forbids inside a
     * write action) says no, and is called on the EDT with neither.
     */
    fun needsWriteAction(key: String): Boolean = true

    /**
     * Generate [key] into [copy], a non-physical copy of the file, for the class at [offset]. Runs in
     * a write action on the EDT, and may change only [copy].
     */
    fun generate(copy: PsiFile, offset: Int, key: String)

    /**
     * The text of a new test class named [testClassName] in package [testPackage] (TestNavigation.kt,
     * "go to test" offering to create one when none exists): one `@Test` stub per public method of the
     * class at [offset] in [file], JUnit 5 Jupiter. Read action. Null if there is no class there.
     */
    fun testSkeleton(file: PsiFile, offset: Int, testClassName: String, testPackage: String): String?

    companion object {
        val EP: ExtensionPointName<Generator> = ExtensionPointName.create("dev.bridge.brain.generator")

        fun forFile(file: PsiFile): Generator? = EP.extensionList.firstOrNull { it.handles(file) }
    }
}
