plugins {
    kotlin("jvm") version "2.4.20"
    kotlin("plugin.spring") version "2.4.20"
    kotlin("plugin.jpa") version "2.4.20"
    id("org.springframework.boot") version "4.1.1"
    id("io.spring.dependency-management") version "1.1.7"
}

group = "dev.bridge"
version = "0.0.1"

java {
    toolchain { languageVersion = JavaLanguageVersion.of(21) }
}

// The fixture's .java probes live alongside their Kotlin equivalents under src/*/kotlin (for the
// features that read them by a shared path), not Gradle's own default src/*/java. IntelliJ's Gradle
// import already treats the folder as accepting both; teach the real compileJava/compileTestJava
// tasks the same, or `./gradlew test` - what the test-running feature (FEATURES.md §6c) actually
// invokes - reports "NO-SOURCE" for them and no Java test is ever found.
sourceSets {
    main { java.srcDir("src/main/kotlin") }
    test { java.srcDir("src/test/kotlin") }
}

repositories { mavenCentral() }

dependencies {
    implementation("org.springframework.boot:spring-boot-starter-web")
    implementation("org.springframework.boot:spring-boot-starter-data-jpa")
    implementation("org.springframework.boot:spring-boot-starter-validation")
    implementation("org.jetbrains.kotlin:kotlin-reflect")
    runtimeOnly("com.h2database:h2")
    testImplementation("org.springframework.boot:spring-boot-starter-test")
}

// Spring Boot's starter-test brings JUnit 5 (Jupiter), but Gradle's `test` task defaults to
// JUnit 4 discovery and silently finds nothing without this - needed for the test-running
// feature (FEATURES.md §6c) to run anything at all via Gradle.
tasks.test {
    useJUnitPlatform()
}

// ResolutionError.kt is a deliberately-broken probe (unresolved references), kept for the
// diagnostics test that proves IntelliJ's daemon, not just its inspections, is the source (SPEC.md
// §9). IntelliJ's own analysis of it is unaffected (that runs off the PSI/daemon, not this task
// graph), but a real `compileKotlin` - which the test-running feature (FEATURES.md §6c) actually
// invokes via Gradle - would otherwise fail to build the whole module over one file that is broken
// on purpose. Excluded here, not fixed there.
tasks.named<org.jetbrains.kotlin.gradle.tasks.KotlinCompile>("compileKotlin") {
    exclude("**/ResolutionError.kt")
}
