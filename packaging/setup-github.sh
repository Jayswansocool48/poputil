#!/usr/bin/env bash
# One-time setup so GitHub builds PopUtil as a Flatpak and serves updates to the COSMIC Store.
# Needs the GitHub CLI logged in (sudo apt install gh && gh auth login). Run it from the poputil folder:
#   packaging/setup-github.sh
# It: creates the public repo Jayswansocool48/poputil (if needed) and pushes this folder, makes a signing key
# for updates and stores it as repository secrets, turns on GitHub Pages, and tags the first release.
set -euo pipefail

OWNER="Jayswansocool48"
REPO="poputil"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_BACKUP="$HOME/.local/share/poputil-signing"
cd "$ROOT"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
command -v gh >/dev/null || { echo "Install the GitHub CLI first: sudo apt install gh && gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Log in first: gh auth login"; exit 1; }
command -v gpg >/dev/null || { echo "gpg is missing: sudo apt install gnupg"; exit 1; }

say "1/4  Repository"
if [ ! -d .git ]; then
  git init -q -b main
  git add -A
  git commit -q -m "feat: PopUtil $(cat VERSION) as a Flatpak"
fi
if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  echo "github.com/$OWNER/$REPO already exists."
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$REPO.git"
  git push -u origin main
else
  # Public, because GitHub Pages (the update server) is free for public repositories.
  gh repo create "$OWNER/$REPO" --public --source . --remote origin --push \
    --description "Control panel for Pop!_OS: find every app on your PC and keep it tidy"
fi

say "2/4  Signing key for updates"
if gh secret list -R "$OWNER/$REPO" 2>/dev/null | grep -q '^GPG_KEY_ID'; then
  echo "Signing secrets are already set."
else
  mkdir -p "$KEY_BACKUP"; chmod 700 "$KEY_BACKUP"
  export GNUPGHOME="$KEY_BACKUP/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
  gpg --batch --quick-gen-key --passphrase '' "PopUtil updates <noreply@users.noreply.github.com>" rsa3072 sign never
  KEY_ID="$(gpg --list-keys --with-colons | awk -F: '/^fpr/ {print $10; exit}')"
  gpg --batch --armor --export-secret-keys "$KEY_ID" | gh secret set GPG_PRIVATE_KEY -R "$OWNER/$REPO"
  gh secret set GPG_KEY_ID -R "$OWNER/$REPO" --body "$KEY_ID"
  echo "Key $KEY_ID stored as repository secrets. A backup is in $KEY_BACKUP (keep it private)."
fi

say "3/4  GitHub Pages"
gh api -X POST "repos/$OWNER/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 \
  || gh api -X PUT "repos/$OWNER/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 || true
echo "Pages is set to publish from GitHub Actions."

say "4/4  First release"
TAG="v$(cat VERSION)"
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "$TAG already exists. To ship a new version: bump VERSION and the release in the metainfo, commit, then"
  echo "  git tag v<new version> && git push origin v<new version>"
else
  git tag "$TAG"
  git push origin "$TAG"
  echo "Tagged $TAG. GitHub is building it now: https://github.com/$OWNER/$REPO/actions"
fi

cat <<EOF

When the build finishes (about 5 minutes), install PopUtil with:
  ./install.sh
or directly:
  flatpak install --user https://jayswansocool48.github.io/poputil/poputil.flatpakref
EOF
