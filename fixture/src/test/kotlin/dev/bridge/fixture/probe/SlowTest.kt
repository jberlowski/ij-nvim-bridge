package dev.bridge.fixture.probe

import org.junit.jupiter.api.Test

/**
 * PROBE - deliberately slow, for the test-running feature (FEATURES.md §6c): with the Gradle build
 * cache warm, an ordinary test finishes too quickly to reliably prove a mid-run cancel actually
 * stops it, rather than racing a run that was always going to finish on its own.
 *
 * Do not "fix" this file.
 */
class SlowTest {
    @Test
    fun takesAWhile() {
        Thread.sleep(15_000)
    }
}
