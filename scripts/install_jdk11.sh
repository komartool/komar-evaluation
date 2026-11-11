#!/usr/bin/env bash
# Install Temurin 11 (the JDK of the cases of cases-jdk11.csv) into <dir>, checksum verified.
#
#   scripts/install_jdk11.sh <dir>        -> <dir>/jdk-11.0.32.1+1 (set it as paths.java_home)
#
# The default setup (scripts/setup.sh, JDK 21) does not need it.
set -euo pipefail
DIR="${1:?usage: scripts/install_jdk11.sh <dir>}"
VERSION="jdk-11.0.32.1+1"
BASE="https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.32.1%2B1"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  FILE=OpenJDK11U-jdk_x64_linux_hotspot_11.0.32.1_1.tar.gz;    SHA=5c3f68887c325d36d852ba534303e1f5f1f5cae7d6cc1e951d73e0d8e98a058d ;;
  Linux-aarch64) FILE=OpenJDK11U-jdk_aarch64_linux_hotspot_11.0.32.1_1.tar.gz; SHA=f27033e6f7523c1b0b2565a78e9c0e0abe5596a854ce00ca04ec1b06ece7a935 ;;
  Darwin-arm64)  FILE=OpenJDK11U-jdk_aarch64_mac_hotspot_11.0.32.1_1.tar.gz;   SHA=c487a1c3a56588b3a32d0d07cdd526bf3d401c4b8422be772ca178bfcd647f3f ;;
  Darwin-x86_64) FILE=OpenJDK11U-jdk_x64_mac_hotspot_11.0.32.1_1.tar.gz;       SHA=03de26f0d11ec3e6365e0352d9fcfaff447be35a2b2f36ee1ffb6063980343f5 ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)"; exit 1 ;;
esac
[ -x "$DIR/$VERSION/bin/java" ] || [ -x "$DIR/$VERSION/Contents/Home/bin/java" ] && { echo "$DIR/$VERSION already installed"; exit 0; }
mkdir -p "$DIR"
tmp=$(mktemp -d "$DIR/.jdk11.XXXXXX"); trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$BASE/$FILE" -o "$tmp/jdk.tgz"
if command -v sha256sum >/dev/null; then got=$(sha256sum "$tmp/jdk.tgz" | cut -d' ' -f1); else got=$(shasum -a 256 "$tmp/jdk.tgz" | cut -d' ' -f1); fi
[ "$got" = "$SHA" ] || { echo "checksum mismatch: $FILE (expected $SHA, got $got)"; exit 1; }
tar -xzf "$tmp/jdk.tgz" -C "$tmp"
mv "$tmp/$VERSION" "$DIR/$VERSION"
echo "installed $DIR/$VERSION"
