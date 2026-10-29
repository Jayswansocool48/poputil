# PopUtil

A control panel for Pop!_OS: see every app on your PC wherever it came from, remove what you don't need
without breaking anything, install apps, apply tweaks, run maintenance, clear disk clutter and organize files.

Ships as a Flatpak. Updates come from this repository through GitHub Pages and show up in the COSMIC Store.

## Install

```bash
flatpak install --user https://jayswansocool48.github.io/poputil/poputil.flatpakref
```

or, from a copy of this repository, `./install.sh`. Either way, the PopUtil update source is added, so new
versions appear under Updates in the COSMIC Store (or run `flatpak update`).

| Command | What it does |
| --- | --- |
| `./install.sh` | Installs PopUtil and removes older non-Flatpak copies (your data stays) |
| `./install.sh update` | Updates to the newest version |
| `./install.sh --check` | Checks PopUtil and what it needs |
| `./install.sh --uninstall` | Removes PopUtil; keeps settings and anything still waiting in Removed |
| `./install.sh --file PopUtil.flatpak` | Installs from a downloaded file (each GitHub release has one) |
| `./install.sh dev` | Runs straight from this folder, for working on PopUtil |

## Publishing (one time)

```bash
sudo apt install gh && gh auth login
packaging/setup-github.sh
```

That creates the public `poputil` repository, pushes this folder, makes a signing key for updates and stores it
as repository secrets (a backup goes to `~/.local/share/poputil-signing`; keep it private), turns on GitHub
Pages, and tags the first release. GitHub Actions then builds the Flatpak and publishes the update site.

### Shipping an update

1. Bump `VERSION` and add a `<release>` to `packaging/flatpak/io.github.Jayswansocool48.PopUtil.metainfo.xml`.
2. Commit, then `git tag v2.2.0 && git push origin main v2.2.0`.

The workflow refuses to build if the tag and `VERSION` disagree. If the GNOME runtime in the manifest
(`runtime-version`) reaches end of life, bump it there.

## How the Flatpak works

PopUtil manages the whole system (apt packages, /opt, services, other Flatpaks), so a sandboxed copy couldn't do
its job. The Flatpak contains the window (GTK 4 + WebKit) and the engine. On launch it copies the engine to
`~/.local/share/poputil/runtime/<version>` and starts it on your system with `flatpak-spawn --host`. The engine
stops when the window closes. Permissions, and why:

- `--talk-name=org.freedesktop.Flatpak`: run the engine outside the sandbox
- `--filesystem=xdg-data/poputil:create`: stage the engine; keep Removed items, scans and organize history
- `--share=network`: the window talks to the engine on 127.0.0.1; AI advice goes to Claude or Ollama if you turn it on

Because of the first one, Flathub wouldn't accept it, which is why it has its own update source.

On first launch PopUtil removes older copies installed with `install.sh` (launchers, `~/.local/bin` links and code).
It never touches your data. Anything owned by root is listed on the Overview with the command to remove it.

## Apps on this PC

PopUtil looks everywhere software ends up on Linux and shows where each app lives, how it got there,
whether it's in the app library, when you last used it and how much space it and its data take:
system packages you installed (with the apt command and date), .deb files, third-party repositories, Flatpak,
Snap, AppImages, /opt, install scripts in /usr/local, files added to /usr/bin, unpacked app folders and loose
programs in your home folder and other drives, pip/pipx/uv/cargo/npm/go tools, toolchains (rustup, nvm, pyenv,
Bun, Deno, SDKMAN, .NET, Android SDK, Go), Unity, Unreal and Godot editors, Wine and Bottles apps, Steam
(plus Proton leftovers), Lutris and Heroic games, background services and broken launchers. If one of those
places can't be read, the scan says so and carries on with the rest.

Removing is planned and shown first. Packages are simulated with apt, and anything Pop!_OS needs is refused.
Open apps are blocked, and the right tool is used for each source. Launchers, links, services and shell-startup
lines go with the app. Files wait in **Removed** until you empty it, and **Restore** puts everything back.

## Design

- Theme: **Meridian Blue** by serafimcloud ([21st.dev](https://21st.dev/community/themes/meridian-blue)), shadcn token names; its orange secondary color is unused
- Layout references: 21st.dev "Dashboard Sidebar" (arunjdass) and "App Dashboard Layout" (shadcnstore), rebuilt without React
- Fonts (self-hosted WOFF2 subsets): Poppins for headings (standing in for the theme's Outfit), Inter for the interface
- Icons: custom SVG, one stroke weight. Motion: spring physics only, generated as CSS `linear()` curves
- Optional: match your COSMIC accent color (Settings)

## Tests

```bash
python3 -m unittest tests.test_parsing          # safe anywhere
tests/fakepc.sh /tmp/pc                          # the rest needs a throwaway machine where you're root
HOME=/tmp/pc/home PATH=/tmp/pc/bin:$PATH python3 -m unittest discover -s tests -v
```
