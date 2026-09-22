package dev.bridge.fixture.probe

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** PROBE - test navigation: the existing test for [Calculator]. Keep the class name matched. */
class CalculatorTest {
    @Test
    fun addsTwoNumbers() {
        assertEquals(5, Calculator().add(2, 3))
    }

    @Test
    fun subtractsTwoNumbers() {
        assertEquals(1, Calculator().subtract(3, 2))
    }
}
