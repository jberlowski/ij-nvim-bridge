package dev.bridge.fixture.probe

/**
 * PROBE — deliberate unresolved symbol.
 *
 * Exists to prove the harness harvests diagnostics from the DaemonCodeAnalyzer
 * and not merely from InspectionEngine. "Cannot resolve" is produced by
 * annotators and highlight visitors, not by inspections, so an inspection-only
 * implementation would report nothing here. See SPEC.md §9.
 *
 * Do not "fix" this file.
 */
class ResolutionError {
    fun brokenCall(): String {
        return thisFunctionDoesNotExistAnywhere()
    }

    fun brokenType(): NoSuchTypeInAnyClasspath? = null
}
