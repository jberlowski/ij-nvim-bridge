# Building from source

Two Gradle projects produce the two plugins: `brain/` (the real one) and `canary/` (the harness's
liveness probe, see HARNESS.md §4). Both build the same way.

## What you need

- **A JDK.** Any reasonably recent one works to *run* Gradle. Compiling targets JDK 21 specifically
  (`kotlin { jvmToolchain(21) }`, matching the IntelliJ platform's own bundled runtime): if the JDK
  you have is not 21, Gradle fetches a JDK 21 toolchain itself the first time, over the network
  (`org.gradle.toolchains.foojay-resolver-convention` in `settings.gradle.kts`) — nothing to
  pre-install. Verified: building with only a JDK 25 present downloads and uses a JDK 21 toolchain
  automatically.
- **No system Gradle.** Both projects carry the Gradle wrapper (`gradlew`, `gradlew.bat`,
  `gradle/wrapper/`) — commit them, use them, never install Gradle by hand.
- **Network access, once**, to fetch: the Gradle distribution the wrapper points at (first run
  only, then cached in `~/.gradle/wrapper`), the Kotlin and IntelliJ Platform Gradle plugins and
  `kotlinx-serialization-json` (Maven Central / the Gradle plugin portal, then cached in
  `~/.gradle/caches`), and the JDK 21 toolchain above if one is not already on the machine. None of
  this is a prebuilt artifact of *this* project — it is ordinary Gradle/JVM tooling, fetched and
  cached the same way any Gradle build fetches its dependencies. Once cached, later builds need no
  network.
- **A local IntelliJ IDEA install**, version 2026.2 (build 262.x) — Ultimate, for the bundled Java
  and Kotlin plugins the Brain compiles against. This is the one thing that cannot come from a
  repository: it is compiled *against* the actual platform jars on disk, not a description of them.
  It does not need to be on `PATH`, and does not need to be at any fixed location.

## Building

```
cd brain
./gradlew buildPlugin -PlocalIdePath="/path/to/your/IntelliJ IDEA install"
# -> build/distributions/brain-0.1.0.zip
```

The install path is almost never the same across machines (a Toolbox install lives under the
user's home directory; on macOS it is the `.app` bundle; a manual install can be anywhere), so it
is a setting, not a default to guess at. Three ways to give it, checked in this order:

1. `-PlocalIdePath=...` on the command line (above).
2. The `IJ_NVIM_BRIDGE_IDEA_HOME` environment variable — the more convenient one when you build the
   same way repeatedly: `export IJ_NVIM_BRIDGE_IDEA_HOME="/path/to/IntelliJ IDEA.app"`.
3. `/opt/idea`, if neither is set — where the development harness's Docker image installs it; not
   meaningful outside it.

`canary/` takes the same two, the same way: `cd canary && ./gradlew buildPlugin`.

`make brain-local` and `make canary-local` are the same commands, for convenience.

## Setting it up

Installing the built plugin into IntelliJ, then the Neovim side, then checking it worked, is
[editor/README.md](./editor/README.md#setup) — including `IJ_NVIM_BRIDGE_IDEA_CMD`, the equivalent
of `IJ_NVIM_BRIDGE_IDEA_HOME` above but for *launching* IntelliJ from `:IjBridge open` rather than
compiling against it.

## The development harness is a different thing

Everything above builds the plugins for your own IntelliJ. The Docker-based test harness
(`make image`, `make canary`, `make brain`, `make test`) is separate: it builds the same two
projects again, but *inside* a container, against a pinned IntelliJ build baked into the image, so
the test suite runs against one known version. See [HARNESS.md](./HARNESS.md). The
`fixture/` Gradle project used only inside that harness is not meant to be built standalone, and
its wrapper is generated at image-build time rather than committed.
