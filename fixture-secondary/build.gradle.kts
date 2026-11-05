plugins { java }
repositories { mavenCentral() }
java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }
dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter:5.11.4")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}
tasks.test { useJUnitPlatform() }
tasks.register("routingProbe") {
    group = "verification"
    doLast {
        val marker = layout.buildDirectory.file("routing-marker").get().asFile
        marker.parentFile.mkdirs()
        marker.writeText(project.name)
        println("ROUTED_TO_" + project.name)
    }
}
tasks.register("routingSlow") {
    group = "verification"
    doLast { println("SLOW_" + project.name); Thread.sleep(30000) }
}
