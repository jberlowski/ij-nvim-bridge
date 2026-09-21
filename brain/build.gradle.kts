plugins {
    kotlin("jvm") version "2.4.20"
    id("org.jetbrains.intellij.platform") version "2.19.0"
}

group = "dev.bridge"
version = "0.1.0"

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    intellijPlatform {
        // Compile against the exact IDE in the harness image (HARNESS.md §9).
        local(providers.gradleProperty("localIdePath").orElse("/opt/idea"))
        // Typed access to Java's and Kotlin's PSI, for generating code. Both are *optional* at run
        // time (plugin.xml): the Brain still loads, without those features, in an IDE that has neither.
        bundledPlugin("com.intellij.java")
        bundledPlugin("org.jetbrains.kotlin")
    }
    // The IDE carries its own kotlinx.serialization, but as a library module with
    // internal visibility: a third-party plugin that depends on it is refused at
    // load ("depends on module ... with internal visibility"). So it is bundled.
    // The Kotlin stdlib is the platform's, so it is excluded from the bundle.
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.9.0") {
        exclude(group = "org.jetbrains.kotlin")
    }
}

kotlin { jvmToolchain(21) }

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            // ADR-0007: the current major, patches need no action.
            sinceBuild = "262"
            untilBuild = "262.*"
        }
    }
    buildSearchableOptions = false
}
