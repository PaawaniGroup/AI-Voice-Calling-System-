#!/bin/bash
# Reproduces the media-fork module build used for the AI bridge (amigniter/mod_audio_stream, AGPL-3.0 + FreeSWITCH linking
# exception). Builds as a normal user; INSTALLING the .so into FreeSWITCH needs root and is a separate, explicit step.
#   bash scripts/build_mod_audio_stream.sh            # build only
#   sudo install -m 0755 $BUILD/mod_audio_stream.so /usr/local/freeswitch/lib/freeswitch/mod/   # install (root)
#   (add <load module="mod_audio_stream"/> to autoload_configs/modules.conf.xml, or `load mod_audio_stream` over ESL)
# Pinned to the commit that was reviewed and built on 2026-10-19; the resulting sha256 is recorded below for comparison.
set -euo pipefail
PIN="ff9cbdc"                                  # 'Add official APT repository installation instructions' (2026-10-07)
SRC="${SRC:-$HOME/mediafork-src}"
FS_PKG="${FS_PKG:-/usr/local/freeswitch/lib/pkgconfig}"
# apt prerequisites (root): libssl-dev zlib1g-dev libevent-dev libspeexdsp-dev cmake g++
mkdir -p "$SRC" && cd "$SRC"
[ -d mod_audio_stream ] || GIT_TERMINAL_PROMPT=0 git clone --recurse-submodules https://github.com/amigniter/mod_audio_stream.git
cd mod_audio_stream
git fetch -q --depth 50 origin 2>/dev/null || true
git checkout -q "$PIN" 2>/dev/null || echo "WARNING: pinned commit $PIN not found; building the checked-out HEAD ($(git rev-parse --short HEAD))"
git submodule update --init --recursive -q
echo "REVIEW the source before installing (no system()/popen()/exec found at the pinned commit)."
export PKG_CONFIG_PATH="$FS_PKG"
rm -rf build && mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DUSE_TLS=ON ..
make -j"$(nproc)"
sha256sum mod_audio_stream.so
echo "reference sha256 (built 2026-10-19, FreeSWITCH 1.11.4-dev, Ubuntu 24.04, gcc 13): 729b85616130687f50380952b26470ca4836ca7c76ecbe4f0c7cbf14fdf345db"
echo "(a different compiler/toolchain will produce a different hash; compare source commit, not just the binary)"
echo "built: $(pwd)/mod_audio_stream.so"
