package dev.bridge.brain

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState

/**
 * Runs [block] on the EDT and returns its value, rethrowing its failure on the
 * calling thread. Modality is explicit: a bare invokeAndWait from a socket
 * thread can land in the wrong context (HARNESS.md §13 - a blocked or
 * mis-scoped EDT looks exactly like a missing API).
 */
fun <T> edt(block: () -> T): T {
    var result: Result<T>? = null
    ApplicationManager.getApplication().invokeAndWait({
        result = runCatching(block)
    }, ModalityState.nonModal())
    return result!!.getOrThrow()
}
