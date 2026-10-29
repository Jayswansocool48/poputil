#!/usr/bin/env bash
# Builds a fake PC for testing the app finder: a fake home folder plus a few system-level installs.
# Usage: tests/fakepc.sh <scratch-dir>      (system parts need root; only for throwaway test machines)
set -euo pipefail
S="${1:?scratch dir}"
F="$S/home"
rm -rf "$F" "$S/bin" "$S/state"; mkdir -p "$F" "$S/bin" "$S/state"

elf() { # elf <path> <MB>: an ELF-looking executable of a given size
  mkdir -p "$(dirname "$1")"; cp /bin/true "$1"; truncate -s "${2}M" "$1"; chmod +x "$1"; }
appimage() { elf "$1" "$2"; printf 'AI\x02' | dd of="$1" bs=1 seek=8 conv=notrunc status=none; }
blob() { mkdir -p "$(dirname "$1")"; head -c "${2}K" /dev/urandom > "$1"; }

# ── fake commands: pkexec runs as-is (tests run as root), flatpak keeps state in a file ──
cat > "$S/bin/pkexec" <<'EOF'
#!/bin/sh
exec "$@"
EOF
cat > "$S/bin/flatpak" <<EOF
#!/bin/bash
ST="$S/state/flatpak"
[ -f "\$ST" ] || printf 'com.spotify.Client\tSpotify\t1.2.31\tstable\tsystem\tflathub\t312.4 MB\n' > "\$ST"
case "\$1 \$2" in
  "list --app") if [[ "\$*" == *description* ]]; then cut -f1 "\$ST" | sed 's/\$/\tOnline music streaming/'; else cat "\$ST"; fi ;;
  "ps --columns=application") ;;
  "uninstall -y") id="\${@: -1}"; grep -v "^\$id	" "\$ST" > "\$ST.n"; mv "\$ST.n" "\$ST"; echo "Uninstalled \$id" ;;
  "install -y") id="\${@: -1}"; printf '%s\tSpotify\t1.2.31\tstable\tsystem\tflathub\t312.4 MB\n' "\$id" >> "\$ST"; echo "Installed \$id" ;;
  *) [ "\$1" = remotes ] && echo flathub ;;
esac
EOF
chmod +x "$S/bin/"*

# ── system-level manual installs ──
rm -rf /opt/FakeEditor-2.1-linux-x64 /usr/local/lib/fakeollama /usr/local/bin/fakeollama /usr/local/bin/fakeeditor \
       /etc/systemd/system/fakeollama.service /usr/share/applications/fakeeditor.desktop
elf /opt/FakeEditor-2.1-linux-x64/fakeeditor 3; blob /opt/FakeEditor-2.1-linux-x64/resources/app.dat 200
ln -s /opt/FakeEditor-2.1-linux-x64/fakeeditor /usr/local/bin/fakeeditor
cat > /usr/share/applications/fakeeditor.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Fake Editor
Comment=Edits fake things
Exec=/usr/local/bin/fakeeditor %F
Icon=fakeeditor
EOF
elf /usr/local/bin/fakeollama 4; mkdir -p /usr/local/lib/fakeollama; blob /usr/local/lib/fakeollama/runner.so 300
cat > /etc/systemd/system/fakeollama.service <<'EOF'
[Unit]
Description=Fake Ollama Service
[Service]
ExecStart=/usr/local/bin/fakeollama serve
EOF

# ── home folder ──
appimage "$F/Applications/Tool-1.6.7-x86_64.AppImage" 3
mkdir -p "$F/.local/share/applications/wine/Programs/Notepad++" "$F/.local/share/icons/hicolor/256x256/apps"
cp /dev/null "$F/.local/share/icons/hicolor/256x256/apps/appimagekit_tool.png"
printf '\x89PNG\r\n\x1a\n' > "$F/.local/share/icons/hicolor/256x256/apps/appimagekit_tool.png"
cat > "$F/.local/share/applications/appimagekit_tool.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Tool Studio
Comment=Makes tools
Exec=$F/Applications/Tool-1.6.7-x86_64.AppImage %U
Icon=appimagekit_tool
EOF
cat > "$F/.local/share/applications/oldgame.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Old Game
Exec=$F/Games/OldGame/run.sh
EOF
elf "$F/Downloads/blender-4.2.0-linux-x64/blender" 6; mkdir -p "$F/Downloads/blender-4.2.0-linux-x64/lib"
elf "$F/Downloads/Godot_v4.3-stable_linux.x86_64" 3
blob "$F/.config/blender/4.2/userpref.blend" 50; blob "$F/.config/godot/editor_settings-4.tres" 10
blob "$F/.config/Tool Studio/settings.json" 20
elf "$F/Unity/Hub/Editor/2022.3.10f1/Editor/Unity" 8
mkdir -p "$F/UE5/Engine/Build"; elf "$F/UE5/Engine/Binaries/Linux/UnrealEditor" 9
echo '{"MajorVersion":5,"MinorVersion":4,"PatchVersion":2}' > "$F/UE5/Engine/Build/Build.version"
# Wine
mkdir -p "$F/.wine/drive_c/Program Files/Common Files"; touch "$F/.wine/system.reg"
blob "$F/.wine/drive_c/Program Files/Notepad++/notepad++.exe" 600
cat > "$F/.local/share/applications/wine/Programs/Notepad++/Notepad++.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Notepad++
Exec=env WINEPREFIX="$F/.wine" wine C:\\\\\\\\users\\\\\\\\Public\\\\\\\\Notepad++.lnk
EOF
# Steam
SA="$F/.local/share/Steam/steamapps"; mkdir -p "$SA/common/MyGame" "$F/.local/share/Steam/userdata/1/config"
blob "$SA/common/MyGame/game.pak" 1500
printf '"AppState"\n{\n\t"appid"\t\t"123"\n\t"name"\t\t"My Game"\n\t"installdir"\t\t"MyGame"\n\t"SizeOnDisk"\t\t"1536000"\n}\n' > "$SA/appmanifest_123.acf"
printf '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s/.local/share/Steam"\n\t}\n}\n' "$F" > "$SA/libraryfolders.vdf"
blob "$SA/compatdata/999/pfx/drive_c/save.dat" 6000
printf '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"123"\n\t\t\t\t\t{\n\t\t\t\t\t\t"LastPlayed"\t\t"1700000000"\n\t\t\t\t\t}\n\t\t\t\t\t"456"\n\t\t\t\t\t{\n\t\t\t\t\t\t"cloud"\n\t\t\t\t\t\t{\n\t\t\t\t\t\t\t"quota"\t\t"1"\n\t\t\t\t\t\t}\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n' > "$F/.local/share/Steam/userdata/1/config/localconfig.vdf"
# Flatpak data (installed + leftover)
blob "$F/.var/app/com.spotify.Client/cache/x" 100; blob "$F/.var/app/com.old.Removed/data/x" 2048
# Dev tools: nvm wired into .bashrc, a cargo tool, ~/.local/bin bits
blob "$F/.nvm/versions/node/v20.11.0/bin/node" 3000; touch "$F/.nvm/nvm.sh"
cat > "$F/.bashrc" <<'EOF'
alias ll='ls -l'
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
EOF
mkdir -p "$F/.cargo/bin"; elf "$F/.cargo/bin/rg" 2
echo '{"installs":{"ripgrep 14.1.0 (registry+https://github.com/rust-lang/crates.io-index)":{"bins":["rg"]}}}' > "$F/.cargo/.crates2.json"
mkdir -p "$F/.local/bin"; printf '#!/bin/sh\necho hi\n' > "$F/.local/bin/mytool"; chmod +x "$F/.local/bin/mytool"
ln -s "$F/gone/dead-tool" "$F/.local/bin/dead"
mkdir -p "$F/.config/systemd/user"
cat > "$F/.config/systemd/user/syncer.service" <<EOF
[Unit]
Description=Syncer
[Service]
ExecStart=$F/Applications/Tool-1.6.7-x86_64.AppImage --daemon
EOF
# a project with a build output that must NOT show up as an app
mkdir -p "$F/Projects/MyGodotGame/build"; touch "$F/Projects/MyGodotGame/project.godot"
elf "$F/Projects/MyGodotGame/build/MyGodotGame.x86_64" 5
find "$F" -exec touch -h -a -d '200 days ago' {} + 2>/dev/null || true
echo "Fake PC ready at $S"
