package dev.bridge.fixture.probe

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/**
 * PROBE - deliberately fails, for the test-running feature (FEATURES.md §6c): proves a failing
 * test really reports `failed`, not just that a passing one reports `passed`.
 *
 * Do not "fix" this file.
 */
class FailingTest {
    @Test
    fun deliberatelyWrong() {
        println("about to fail")
        System.err.println("a complaint on stderr")
        assertEquals(1, 2)
    }
}
