# The harness is a Mac-local container and never runs on the target machine

The harness is an OCI container run via OrbStack on the development Mac. It exists solely to provide an IntelliJ and a Neovim that can talk, resettable and observable, so features can be built in a loop. It is not a deployment artifact and deliberately does not run on the WSL2 box, which is the production target: a licensed IntelliJ on WSLg plus a native Neovim.

## Considered options

**A Lima or OrbStack Linux machine** was chosen first, for systemd, a real session and a real `/run/user/1000` — closer to the WSLg environment. It was reconsidered because on macOS a container and a VM both run inside a Linux VM anyway, so the choice is about artifact shape rather than virtualisation, and the container wins on reset speed (discard versus snapshot-restore) and on layer-cached rebuilds after a plugin change.

**Making the harness portable to the WSL box** was pursued for two rounds of design and was a misreading. It produced an elaborate two-layer sudo-free provisioning scheme solving a problem nobody had.

## Consequences

The "no sudo" requirement is a constraint on the **Bridge**, not on the harness: Bridge communication must never need elevation, which is why the Registry lives in `$XDG_RUNTIME_DIR` with a `$HOME` fallback and why there is no daemon and no privileged port. It says nothing about how a test environment installs software.

Verification on the real WSL box is manual, by the developer. The harness proves features work; it does not prove they work *there*.

Architecture differences between the arm64 Mac and the x86_64 WSL box are not a risk: both IntelliJ and Neovim ship native builds for each, and the speed gate measures overhead rather than absolute latency ([ADR-0005](./0005-gate-on-bridge-overhead-not-latency.md)), so it means the same thing on both.

Neither Lima nor OrbStack provides a display, so Xvfb plus noVNC lives inside the image — which also makes the harness screenshotable, something a host X server would not give as cleanly.
