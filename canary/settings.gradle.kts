plugins {
    // Compiling targets JDK 21 (the platform's own bundled runtime). A machine whose only JDK is
    // newer (or older) still builds: this fetches a JDK 21 toolchain over the network rather than
    // requiring one to be pre-installed. Needs network on first use; nothing to pre-stage.
    id("org.gradle.toolchains.foojay-resolver-convention") version "1.0.0"
}

rootProject.name = "canary"
