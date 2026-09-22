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
        // Build against a real IDE install rather than downloading a second SDK: the harness pins
        // one build (HARNESS.md §9) and the plugin should be compiled against that same one. See
        // brain/build.gradle.kts for how to point this at an install that is not at /opt/idea.
        local(providers.gradleProperty("localIdePath")
            .orElse(providers.environmentVariable("IJ_NVIM_BRIDGE_IDEA_HOME"))
            .orElse("/opt/idea"))
    }
}

kotlin { jvmToolchain(21) }

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            sinceBuild = "262"
            untilBuild = "262.*"
        }
    }
    buildSearchableOptions = false
}
