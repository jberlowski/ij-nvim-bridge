package dev.bridge.fixture.probe

/**
 * PROBE — half of the unsaved cross-file scenario (SPEC.md §5.2, HARNESS.md §7).
 *
 * A test adds a method here WITHOUT saving, then asks the Brain to resolve a
 * call to it from CrossFileConsumer.kt. If the Mirror Set were only the active
 * buffer, the Brain would answer from the stale disk copy and report
 * "cannot resolve" — the exact failure the Mirror Set rule exists to prevent.
 *
 * The marker comment is an insertion point for the test. Do not remove it.
 */
class CrossFileProducer {

    fun existingMethod(): String = "already on disk"

    // HARNESS-INSERTION-POINT
}
