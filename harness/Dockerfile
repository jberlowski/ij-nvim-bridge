# Harness image. See ../HARNESS.md
#
# Build from the REPO ROOT so the fixture is in context:
#     docker build -f harness/Dockerfile -t ij-nvim-harness:base .
#
# Versions are pinned deliberately (HARNESS.md §9): the supported IntelliJ
# *range* is a policy, but a reproducible test environment needs a fixed
# point, or a rebuilt image changes behaviour with nothing recording why.

FROM ubuntu:24.04

ARG IDEA_VERSION=2026.2.3
ARG IDEA_BUILD=262.10968.63
ARG NVIM_VERSION=v0.12.5
ARG GRADLE_VERSION=9.7.1
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    DISPLAY=:99

# ---------------------------------------------------------------- packages
# Split from the big downloads so dependency changes do not re-fetch IntelliJ.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git unzip xz-utils procps psmisc socat \
      python3 python3-venv python3-pip \
      xvfb x11vnc novnc websockify xterm x11-utils xdotool imagemagick \
      libxext6 libxrender1 libxtst6 libxi6 libxkbcommon0 libxrandr2 \
      libfreetype6 fontconfig fonts-dejavu-core fonts-jetbrains-mono \
      libsecret-1-0 libnss3 libdrm2 libgbm1 \
      openjdk-21-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- intellij
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) IDEA_ARCHIVE="idea-${IDEA_VERSION}-aarch64.tar.gz" ;; \
      amd64) IDEA_ARCHIVE="idea-${IDEA_VERSION}.tar.gz" ;; \
      *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/idea.tar.gz "https://download.jetbrains.com/idea/${IDEA_ARCHIVE}"; \
    curl -fsSL -o /tmp/idea.sha256 "https://download.jetbrains.com/idea/${IDEA_ARCHIVE}.sha256"; \
    cd /tmp && awk '{print $1"  /tmp/idea.tar.gz"}' idea.sha256 | sha256sum -c -; \
    mkdir -p /opt/idea && tar -xzf /tmp/idea.tar.gz -C /opt/idea --strip-components=1; \
    rm -f /tmp/idea.tar.gz /tmp/idea.sha256; \
    test -x /opt/idea/bin/idea.sh || test -x /opt/idea/bin/idea; \
    grep -q "${IDEA_BUILD}" /opt/idea/build.txt || { \
      echo "expected build ${IDEA_BUILD}, got $(cat /opt/idea/build.txt)" >&2; exit 1; }

# ---------------------------------------------------------------- neovim
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) NVIM_ARCHIVE="nvim-linux-arm64.tar.gz" ;; \
      amd64) NVIM_ARCHIVE="nvim-linux-x86_64.tar.gz" ;; \
    esac; \
    curl -fsSL -o /tmp/nvim.tar.gz \
      "https://github.com/neovim/neovim/releases/download/${NVIM_VERSION}/${NVIM_ARCHIVE}"; \
    mkdir -p /opt/nvim && tar -xzf /tmp/nvim.tar.gz -C /opt/nvim --strip-components=1; \
    rm -f /tmp/nvim.tar.gz; \
    /opt/nvim/bin/nvim --version | head -1

# ---------------------------------------------------------------- gradle
RUN set -eux; \
    curl -fsSL -o /tmp/gradle.zip \
      "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip"; \
    unzip -q /tmp/gradle.zip -d /opt && mv "/opt/gradle-${GRADLE_VERSION}" /opt/gradle; \
    rm -f /tmp/gradle.zip; /opt/gradle/bin/gradle --version | grep Gradle

# ---------------------------------------------------------------- window mgr
# A window manager is not optional. Without one, X11 clients get no geometry
# management at all: IntelliJ's frame renders as a ~40px sliver, nothing can be
# raised or resized, and opening an editor deadlocks the EDT. i3 also tiles, so
# the IDE and the terminal sit side by side in the live view instead of one
# covering the other.
#
# Installed in its own layer, after the large downloads, so adding it does not
# invalidate the cached IntelliJ and Neovim layers.
RUN apt-get update && apt-get install -y --no-install-recommends \
      i3-wm \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/nvim/bin:/opt/gradle/bin:${PATH}

# ---------------------------------------------------------------- user
# Unprivileged, uid 1000, with a real XDG_RUNTIME_DIR — the Registry lives
# there (SPEC.md §4.2) and the fallback path must not be what we exercise
# by accident.
RUN userdel -r ubuntu 2>/dev/null || true; \
    useradd -m -u 1000 -s /bin/bash dev; \
    mkdir -p /run/user/1000 && chown dev:dev /run/user/1000 && chmod 700 /run/user/1000; \
    mkdir -p /work && chown dev:dev /work

COPY harness/rootfs/ /

USER dev
WORKDIR /home/dev

ENV XDG_RUNTIME_DIR=/run/user/1000 \
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-arm64

# ------------------------------------------------- JetBrains User Agreement
# PROVENANCE: the repository owner accepted the JetBrains User Agreement
# (version 2.0) interactively, in this container, via noVNC, on 2026-09-20.
# The values below were read back from that session — they are a record of
# their acceptance, not a substitute for it.
#
# There is no supported non-interactive path: the only *NON_INTERACTIVE flag
# in the product is REMOTE_DEV_NON_INTERACTIVE, which covers shell prompts on
# the headless remote-dev backend, and JetBrains support state plainly that no
# command-line option exists.
#
# Deliberately NOT baked: device_id / user_id_on_machine. Those are per-machine
# identifiers and are left to regenerate per container.
#
# consentOptions ":0:" declines usage statistics.
RUN set -eux; \
    PP="$(printf 'XyEoISFjZyJwISh9IX1AImohKGshfHcidyEnOCFiISJwISc6IWVAPT0=' | base64 -d)"; \
    mkdir -p "$HOME/.java/.userPrefs/jetbrains/$PP"; \
    { echo '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'; \
      echo '<!DOCTYPE map SYSTEM "http://java.sun.com/dtd/preferences.dtd">'; \
      echo '<map MAP_XML_VERSION="1.0">'; \
      echo '  <entry key="eua_accepted_version" value="2.0"/>'; \
      echo '</map>'; } > "$HOME/.java/.userPrefs/jetbrains/$PP/prefs.xml"; \
    mkdir -p "$HOME/.local/share/JetBrains/consentOptions"; \
    printf 'rsch.send.usage.stat:1.1:0:%s' "$(date +%s)000" \
      > "$HOME/.local/share/JetBrains/consentOptions/accepted"

# ---------------------------------------------------------------- ide tuning
# Indexing a Spring/Gradle project needs more than the stock 2 GB heap.
RUN IDECFG="$HOME/.config/JetBrains/IntelliJIdea2026.2" && \
    mkdir -p "$IDECFG/options" && \
    printf '%s\n' '-Xms1g' '-Xmx4g' '-XX:+UseG1GC' \
      '-Djava.awt.headless=false' \
      > "$IDECFG/idea64.vmoptions" && \
    \
    # First-run onboarding blocks the EDT. The "Meet the Islands Theme" tour
    # popup made openTextEditor() from a plugin hang indefinitely — a deadlock
    # that looked like an IntelliJ API limitation until a screenshot showed the
    # dialog sitting there.
    #
    # The switch that matters is the Registry key ide.experimental.ui.onboarding,
    # read by NewUiOnboardingUtil.isOnboardingEnabled. The PropertiesComponent
    # value below is what dismissing the dialog writes, which is NOT the same
    # thing and does not suppress it on a first run.
    printf '%s\n' \
      '<application>' \
      '  <component name="Registry">' \
      '    <entry key="ide.experimental.ui.onboarding" value="false" source="USER" />' \
      '    <entry key="switched.from.classic.to.islands" value="true" source="SYSTEM" />' \
      '    <entry key="ide.experimental.ui" value="true" source="SYSTEM" />' \
      '  </component>' \
      '</application>' \
      > "$IDECFG/options/ide.general.xml" && \
    printf '%s\n' \
      '<application>' \
      '  <component name="AIOnboardingPromoWindowAdvisor">' \
      '    <option name="attempts" value="1" />' \
      '  </component>' \
      '</application>' \
      > "$IDECFG/options/AIOnboardingPromoWindowAdvisor.xml" && \
    printf '%s\n' \
      '<application>' \
      '  <component name="PropertiesComponent">' \
      '    <property name="experimental.ui.onboarding.proposed.version" value="2026.2" />' \
      '    <property name="ide.show.tips.on.startup" value="false" />' \
      '  </component>' \
      '</application>' \
      > "$IDECFG/options/other.xml"

# ---------------------------------------------------------------- lazyvim
# The real editor config, not a hand-rolled minimal one: the Bridge ships a
# blink.cmp source and blink is LazyVim's default (SPEC.md §13), so the
# harness must exercise what is actually used.
ARG LAZYVIM_STARTER_REF=main
RUN git clone --depth 1 --branch "${LAZYVIM_STARTER_REF}" \
      https://github.com/LazyVim/starter "$HOME/.config/nvim" && \
    rm -rf "$HOME/.config/nvim/.git" && \
    nvim --headless "+Lazy! sync" +qa 2>&1 | tail -5 || true && \
    test -d "$HOME/.local/share/nvim/lazy/blink.cmp" \
      || { echo "blink.cmp did not install" >&2; exit 1; }

# ---------------------------------------------------------------- fixture
COPY --chown=dev:dev fixture/ /work/fixture/
RUN cd /work/fixture && \
    gradle wrapper --gradle-version "${GRADLE_VERSION}" --no-daemon -q && \
    ./gradlew --no-daemon -q dependencies --configuration compileClasspath \
      > /dev/null 2>&1 || true

EXPOSE 6080 5900

ENTRYPOINT ["/usr/local/bin/harness-init"]
CMD ["sleep", "infinity"]
