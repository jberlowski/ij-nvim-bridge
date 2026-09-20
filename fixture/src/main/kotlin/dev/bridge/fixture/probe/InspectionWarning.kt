package dev.bridge.fixture.probe

/**
 * PROBE — warnings only, no errors.
 *
 * Compiles cleanly. Exists to prove severity mapping: IntelliJ HighlightSeverity
 * to LSP DiagnosticSeverity. If everything arrives as ERROR, or these are absent
 * while ResolutionError.kt reports, the mapping is wrong. See SPEC.md §9.
 *
 * Do not "fix" this file.
 */
@Suppress("unused")
class InspectionWarning {

    fun unusedLocal(): Int {
        val neverRead = expensiveComputation()
        return 1
    }

    fun redundantElvis(value: String): String {
        return value ?: "unreachable default"
    }

    fun alwaysTrueComparison(count: Int): Boolean {
        if (count == count) return true
        return false
    }

    private fun expensiveComputation(): Int = (1..100).sum()
}
