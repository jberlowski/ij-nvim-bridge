package dev.bridge.fixture.probe;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

/** PROBE - test navigation: the existing test for {@link Multiplier}. Keep the class name matched. */
public class MultiplierTest {
    @Test
    void multipliesTwoNumbers() {
        assertEquals(6, new Multiplier().multiply(2, 3));
    }
}
