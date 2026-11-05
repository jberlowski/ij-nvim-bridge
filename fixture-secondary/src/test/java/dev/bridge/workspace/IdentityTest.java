package dev.bridge.workspace;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

public class IdentityTest {
    @Test public void identifiesBuild() {
        System.out.println("TEST_IN_B");
        assertEquals("B", new Identity().identify());
    }
}
