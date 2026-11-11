#!/usr/bin/env bash
# Install Komar (github.com/komartool/komar), the observation tool of the komar mode (docs/komar-mode.md),
# into <dir>: the commit of the config (komar_commit; KOMAR_COMMIT in the environment overrides it), built
# from source with a pinned Go and Gradle and the config's JDK 21+.
#
#   scripts/setup_komar.sh <config.yaml> [<dir>]     (<dir>: default paths.komar, else <paths.tools>/komar)
#
# Result: <dir>/bin/komar, <dir>/komar-agent.jar (the agent jar the binary embeds, extracted; the mode
# passes it with KOMAR_AGENT_JAR so that nothing is written to the user cache), <dir>/versions.json
# (commit, Go, Gradle, checksums of binary and jar; part of the input hash of the komar cells).
# The default setup (scripts/setup.sh) does not run this script. Linux x86_64 only; needs git, curl,
# python3. Downloads are checksum-verified (Go modules by go.sum); rerunning skips what is installed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${1:?usage: scripts/setup_komar.sh <config.yaml> [<dir>]}"
cd "$ROOT"

eval "$(python3 - "$CONFIG" <<'EOF'
import shlex, sys
from runner.config import load_config
c = load_config(sys.argv[1])
for k, v in {"CFG_DIR": c.komar_dir, "JAVA_HOME_CFG": c.java_home, "CFG_COMMIT": c.komar_commit}.items():
    print(f"{k}={shlex.quote(str(v))}")
EOF
)"
DIR="${2:-$CFG_DIR}"
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"

[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || { echo "komar: Linux x86_64 only"; exit 1; }

KOMAR_URL="${KOMAR_URL:-https://github.com/komartool/komar.git}"
KOMAR_COMMIT="${KOMAR_COMMIT:-$CFG_COMMIT}"
[[ "$KOMAR_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "komar: KOMAR_COMMIT must be a full commit hash"; exit 1; }
GO_VERSION=go1.26.10
GO_URL=https://go.dev/dl/go1.26.10.linux-amd64.tar.gz
GO_SHA=ccfe7ae822d2c2f41f4fe7c956199967a4dac22d0ee0d2b2bfdb440aab3b20c5
# the version of the project's Gradle wrapper (runtimes/jvm/agent/gradle/wrapper), run directly
GRADLE_VERSION=9.8.2
GRADLE_URL=https://services.gradle.org/distributions/gradle-9.8.2-bin.zip
GRADLE_SHA=21bd5da2de49b642904077f38c0efdc7778e92b3ed0889392dca61b97e389607

JAVA_HOME="${KOMAR_BUILD_JDK:-$JAVA_HOME_CFG}"
"$JAVA_HOME/bin/java" -version 2>&1 | grep -qE 'version "(2[1-9]|[3-9][0-9])' \
  || { echo "komar: building needs JDK 21+ (the config's java_home or KOMAR_BUILD_JDK)"; exit 1; }
export JAVA_HOME

if [ -f "$DIR/versions.json" ] && python3 -c "import json,sys; sys.exit(json.load(open('$DIR/versions.json'))['commit'] != '$KOMAR_COMMIT')"; then
  echo "komar $KOMAR_COMMIT already installed in $DIR"; exit 0
fi

B="$DIR/build"
rm -rf "$B" && mkdir -p "$B"
trap 'rm -rf "$B"' EXIT
fetch() {  # fetch <url> <sha256> <dest-file>
  curl -fsSL "$1" -o "$3"
  [ "$(sha256sum "$3" | cut -d' ' -f1)" = "$2" ] || { echo "checksum mismatch: $1"; exit 1; }
}

echo "== Go $GO_VERSION, Gradle $GRADLE_VERSION"
fetch "$GO_URL" "$GO_SHA" "$B/go.tgz" && tar -xzf "$B/go.tgz" -C "$B"
fetch "$GRADLE_URL" "$GRADLE_SHA" "$B/gradle.zip"
python3 -c "
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for i in z.infolist():
        p = z.extract(i, sys.argv[2]); m = (i.external_attr >> 16) & 0o777
        m and os.chmod(p, m)" "$B/gradle.zip" "$B"

echo "== Komar $KOMAR_COMMIT"
git clone -q "$KOMAR_URL" "$B/src" && git -C "$B/src" checkout -q "$KOMAR_COMMIT"
export GOPATH="$B/gopath" GOCACHE="$B/gocache" GOFLAGS="-mod=readonly -modcacherw" GRADLE_USER_HOME="$B/gradle-home"
export PATH="$B/go/bin:$JAVA_HOME/bin:$PATH"
(cd "$B/src/runtimes/jvm/agent" && "$B/gradle-$GRADLE_VERSION/bin/gradle" --quiet --no-daemon shadowJar)
cp "$B/src/runtimes/jvm/agent/build/libs/komar-agent.jar" "$B/src/internal/runtime/jvm/komar-agent.jar"
(cd "$B/src" && go build -trimpath -o "$B/komar" ./cmd/komar)

mkdir -p "$DIR/bin"
mv "$B/komar" "$DIR/bin/komar"
cp "$B/src/internal/runtime/jvm/komar-agent.jar" "$DIR/komar-agent.jar"
python3 - "$DIR" "$KOMAR_COMMIT" "$GO_VERSION" "$GRADLE_VERSION" "$("$JAVA_HOME/bin/java" -version 2>&1 | head -1)" <<'EOF'
import hashlib, json, sys
from pathlib import Path
d, commit, go, gradle, jdk = sys.argv[1:]
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
(Path(d) / "versions.json").write_text(json.dumps({
    "commit": commit, "go": go, "gradle": gradle, "build_jdk": jdk,
    "binary_sha256": sha(f"{d}/bin/komar"), "agent_jar_sha256": sha(f"{d}/komar-agent.jar")}, indent=2) + "\n")
EOF
"$DIR/bin/komar" --version 2>/dev/null || true
echo "installed in $DIR"
