package dev.bridge.fixture.probe

/**
 * PROBE — the other half of the unsaved cross-file scenario.
 * See CrossFileProducer.kt.
 */
class CrossFileConsumer(private val producer: CrossFileProducer) {

    fun callExisting(): String = producer.existingMethod()

    // A test appends a call to the unsaved method here and asserts it resolves.
}
