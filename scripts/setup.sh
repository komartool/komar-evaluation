#!/usr/bin/env bash
# Install the tools into .tools/ (or paths.tools of the config) and prepare the subject projects.
#
#   scripts/setup.sh [config.yaml]
#
#   1. checks the JDK of the config (`jdk`, 21 by default; paths.java_home or JAVA_HOME), Maven 3.9, git,
#      curl, python3 with PyYAML (the cases of cases-jdk11.csv run on JDK 11: scripts/install_jdk11.sh);
#   2. Node.js 22 (checksum verified) and Pi (pinned version; its npm-shrinkwrap.json pins the
#      dependency tree) into <tools>/node and <tools>/pi, unless already there;
#   3. BTrace 2.2.6 into <tools>/btrace;
#   4. the JDI server of the autonomous mode (pi/debugger/JdiDapServer.java) into <tools>/debugger;
#   5. (cases of cases-jdk11.csv) JDK 11: scripts/install_jdk11.sh <dir>, then a config with `jdk: 11`,
#      paths.java_home = <dir>/jdk-11.0.32.1+1 and `case_list: cases-jdk11.csv`; the steps below are
#      the same (BTrace, the debugger compiled with that JDK);
#   6. clones the subject projects of the config's case list (cases.csv, or case_list of the config) at
#      the commits of projects.yaml into paths.projects and builds them
#      (scripts/prepare_projects.py): the case commands run offline afterwards; projects whose
#      build already succeeded at the pinned commit are not rebuilt (--rebuild to force).
#
# Downloads go to a temporary file and are moved into place only after the checksum and the
# extraction succeed, so an interrupted run leaves nothing half-installed; just run it again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${1:-$ROOT/config.yaml}"
NODE_VERSION=22.17.0
PI_VERSION=0.84.1
BTRACE_VERSION=2.2.6
BTRACE_SHA256=aaa5ce8d5c4493775a11b2526d989d6e8444a14508de797fb0f99f9bcc574f82
# https://nodejs.org/dist/v22.17.0/SHASUMS256.txt
NODE_SHA256_linux_x64=0fa01328a0f3d10800623f7107fbcd654a60ec178fab1ef5b9779e94e0419e1a
NODE_SHA256_linux_arm64=3e99df8b01b27dc8b334a2a30d1cd500442b3b0877d217b308fd61a9ccfc33d4
NODE_SHA256_darwin_arm64=615dda58b5fb41fad2be43940b6398ca56554cbe05800953afadc724729cb09e
NODE_SHA256_darwin_x64=c39c8ec3cdadedfcc75de0cb3305df95ae2aecebc5db8d68a9b67bd74616d2ad

cd "$ROOT"
python3 -c 'import yaml' 2>/dev/null || { echo "python3 with PyYAML required (pip install pyyaml)"; exit 1; }

TMP_DIRS=()
trap 'rm -rf "${TMP_DIRS[@]:-}"' EXIT

sha256_check() {  # sha256_check <expected> <file>
  local got
  if command -v sha256sum >/dev/null; then got=$(sha256sum "$2" | cut -d' ' -f1)
  else got=$(shasum -a 256 "$2" | cut -d' ' -f1); fi
  [ "$got" = "$1" ] || { echo "checksum mismatch: $2 (expected $1, got $got)"; return 1; }
}

eval "$(python3 - "$CONFIG" <<'EOF'
import shlex, sys
from runner.config import load_config
c = load_config(sys.argv[1])
for k, v in {"TOOLS": c.tools_dir, "JAVA_HOME": c.java_home, "M2_REPO": c.maven_repository,
             "PROJECTS": c.projects_dir, "JDK": c.jdk}.items():
    print(f"{k}={shlex.quote(str(v))}")
EOF
)"
export JAVA_HOME PATH="$JAVA_HOME/bin:$PATH"

echo "== checks"
"$JAVA_HOME/bin/java" -version 2>&1 | head -1 | grep -q "\"$JDK[.\"]" || { echo "JDK $JDK required at $JAVA_HOME"; exit 1; }
command -v mvn >/dev/null || { echo "Maven (mvn) required on PATH"; exit 1; }
MAVEN_VERSION=$(mvn -v 2>/dev/null | sed -n 's/^Apache Maven \([0-9.]*\).*/\1/p' | head -1)
case "$MAVEN_VERSION" in 3.9.*) ;; *) echo "Maven 3.9 required, found '${MAVEN_VERSION:-none}'"; exit 1 ;; esac
for tool in git curl tar; do command -v "$tool" >/dev/null || { echo "$tool required"; exit 1; }; done
if command -v sha256sum >/dev/null || command -v shasum >/dev/null; then :; else echo "sha256sum or shasum required"; exit 1; fi
echo "JDK: $("$JAVA_HOME/bin/java" -version 2>&1 | head -1); Maven $MAVEN_VERSION"
mkdir -p "$TOOLS"
rm -rf "$TOOLS"/.node.* "$TOOLS"/.btrace.*  # leftovers of an interrupted run

echo "== Node.js $NODE_VERSION and Pi $PI_VERSION"
if [ ! -x "$TOOLS/node/bin/node" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) NODE_PLATFORM=linux-x64 ;;
    Linux-aarch64) NODE_PLATFORM=linux-arm64 ;;
    Darwin-arm64) NODE_PLATFORM=darwin-arm64 ;;
    Darwin-x86_64) NODE_PLATFORM=darwin-x64 ;;
    *) echo "unsupported platform $(uname -s)-$(uname -m)"; exit 1 ;;
  esac
  node_sha_var="NODE_SHA256_${NODE_PLATFORM//-/_}"
  tmp=$(mktemp -d "$TOOLS/.node.XXXXXX"); TMP_DIRS+=("$tmp")
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-$NODE_PLATFORM.tar.gz" -o "$tmp/node.tgz"
  sha256_check "${!node_sha_var}" "$tmp/node.tgz"
  mkdir "$tmp/node" && tar -xzf "$tmp/node.tgz" -C "$tmp/node" --strip-components 1
  rm -rf "$TOOLS/node" && mv "$tmp/node" "$TOOLS/node" && rm -rf "$tmp"
fi
export PATH="$TOOLS/node/bin:$PATH"
if [ "$("$TOOLS/pi/bin/pi" --version 2>/dev/null | tail -1)" != "$PI_VERSION" ]; then
  rm -rf "$TOOLS/pi"
  npm install --global --prefix "$TOOLS/pi" --cache "$TOOLS/npm-cache" --no-fund --no-audit --no-update-notifier \
    "@earendil-works/pi-coding-agent@$PI_VERSION"
fi
"$TOOLS/pi/bin/pi" --version | tail -1
[ -f "$TOOLS/pi/lib/node_modules/@earendil-works/pi-coding-agent/npm-shrinkwrap.json" ] || {
  echo "Pi $PI_VERSION has no npm-shrinkwrap.json: its dependency tree is not pinned"; exit 1; }

echo "== BTrace $BTRACE_VERSION"
btrace_ok() { for j in agent boot client; do [ -f "$TOOLS/btrace/libs/btrace-$j.jar" ] || return 1; done; }
if ! btrace_ok; then
  tmp=$(mktemp -d "$TOOLS/.btrace.XXXXXX"); TMP_DIRS+=("$tmp")
  curl -fsSL "https://github.com/btraceio/btrace/releases/download/v$BTRACE_VERSION/btrace-v$BTRACE_VERSION-bin.tar.gz" -o "$tmp/btrace.tgz"
  sha256_check "$BTRACE_SHA256" "$tmp/btrace.tgz"
  mkdir "$tmp/btrace" && tar -xzf "$tmp/btrace.tgz" -C "$tmp/btrace"
  rm -rf "$TOOLS/btrace" && mv "$tmp/btrace" "$TOOLS/btrace" && rm -rf "$tmp"
  btrace_ok || { echo "unexpected BTrace archive layout: libs/btrace-{agent,boot,client}.jar missing"; exit 1; }
fi

echo "== debugger"
python3 -m runner.debugger --config "$CONFIG"

echo "== projects in $PROJECTS"
python3 scripts/prepare_projects.py --config "$CONFIG" "${@:2}"
echo "setup done"
