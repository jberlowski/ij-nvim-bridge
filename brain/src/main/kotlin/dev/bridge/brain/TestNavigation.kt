package dev.bridge.brain

import com.intellij.openapi.module.ModuleUtilCore
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.PsiNameIdentifierOwner
import com.intellij.testIntegration.TestFinderHelper
import com.intellij.testIntegration.createTest.CreateTestUtils
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.nio.file.Path

/**
 * Go to test / go to the class under test (FEATURES.md §6c): IntelliJ's own Go to Test
 * (Ctrl+Shift+T), language-agnostic in the platform (`TestFinderHelper`), so it works the same
 * way for Kotlin and Java, and for anything else a test-framework plugin registers a finder for.
 *
 * One request, one direction chosen automatically by what the caret is in: from a class, its
 * test(s); from a test, the class it tests. When a production class has no test yet, the answer
 * offers to create one - a skeleton with one `@Test` stub per public method (JUnit 5 Jupiter),
 * built the same way as `source.generate.*` (D2: data first, nothing written until the Editor
 * writes it), reusing [Generator] for the language-specific part.
 *
 * Not yet: from a test *method* to the specific method it tests by naming convention (`testAdd`
 * -> `add`) - only class-level navigation; interactively choosing which methods to stub, a
 * superclass or setUp/tearDown, all of which IntelliJ's own Create Test dialog asks about and
 * which have no non-interactive equivalent worth building headless.
 */
class TestNavigation(private val project: Project, private val locations: Locations) {

    /** Must run in a read action. */
    fun targets(mirror: Mirror, offset: Int): JsonElement {
        val doc = mirror.document
        val psiFile = PsiDocumentManager.getInstance(project).getPsiFile(doc) ?: return JsonNull
        val element = psiFile.findElementAt(offset) ?: psiFile.findElementAt((offset - 1).coerceAtLeast(0)) ?: return JsonNull
        val source = TestFinderHelper.findSourceElement(element) ?: return JsonNull
        val isTest = TestFinderHelper.isTest(source)
        val related = (if (isTest) TestFinderHelper.findClassesForTest(source) else TestFinderHelper.findTestsForClass(source))
            .distinct()

        if (related.isNotEmpty()) {
            return buildJsonObject {
                put("kind", if (isTest) "class" else "test")
                put("locations", JsonArray(related.mapNotNull { locations.of(it) }))
            }
        }
        if (isTest) return JsonNull // a test with no class found: nothing to offer instead

        val created = createOffer(psiFile, source)
        return buildJsonObject {
            put("kind", "test")
            put("locations", JsonArray(emptyList()))
            if (created != null) put("create", created)
        }
    }

    /** What a new test for [source] (declared in [file]) would be, if IntelliJ can place one. Null if
     * the file's module has no suitable test source root, or no language [Generator] handles it. */
    private fun createOffer(file: PsiFile, source: PsiElement): JsonObject? {
        val virtual = file.virtualFile ?: return null
        val module = ModuleUtilCore.findModuleForFile(file) ?: return null
        val sourceRoot = com.intellij.openapi.roots.ProjectFileIndex.getInstance(project).getSourceRootForFile(virtual) ?: return null

        // The test root that mirrors this one's own source set - "kotlin" to "kotlin", "java" to
        // "java" - not just whichever candidate happens to come first (a module can have both).
        // computeTestRoots finds the real, existing ones; computeSuitableTestRootUrls is for
        // suggesting a *new* one to create and is empty once a real test root already exists.
        val candidates = CreateTestUtils.computeTestRoots(module).map { Path.of(it.path) }
        val testRoot = candidates.firstOrNull { it.fileName == Path.of(sourceRoot.path).fileName } ?: candidates.firstOrNull() ?: return null

        val extension = virtual.extension ?: return null
        val className = (source as? PsiNameIdentifierOwner)?.name ?: return null
        val testClassName = "${className}Test"

        val rootPath = Path.of(sourceRoot.path)
        val dirPath = Path.of(virtual.parent.path)
        val relativePackageDir = if (dirPath.startsWith(rootPath)) rootPath.relativize(dirPath).toString().replace('\\', '/') else ""
        val targetDir = if (relativePackageDir.isEmpty()) testRoot else testRoot.resolve(relativePackageDir)
        val targetFile = targetDir.resolve("$testClassName.$extension")
        if (LocalFileSystem.getInstance().findFileByNioFile(targetFile) != null) return null // exists on disk, not yet indexed

        val generator = Generator.forFile(file) ?: return null
        val testPackage = relativePackageDir.replace('/', '.')
        val text = generator.testSkeleton(file, source.textOffset, testClassName, testPackage) ?: return null

        return buildJsonObject {
            put("uri", targetFile.toUri().toString())
            put("package", testPackage)
            put("className", testClassName)
            put("text", text)
        }
    }
}
