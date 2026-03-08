#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# moonlake_package.sh
#
# Builds the Flutter engine for required targets and packages the SDK into
# flutter_sdk_linux.tar.gz for distribution to app developers.
#
# Usage: flutter run --local-engine-host=host_debug --local-engine=android_debug_arm64
#
# After extracting, developers must:
#   1. git config --global --add safe.directory <path-to-flutter>
#   2. flutter precache  (fetches dart-sdk, web-sdk, fonts, gradle)
#   3. flutter run --local-engine-host=host_debug --local-engine=<target>
# ---------------------------------------------------------------------------

VERSION_STRING="3.42.0-moonlake"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_SRC="$SCRIPT_DIR/engine/src"
OUT="$ENGINE_SRC/out"
CACHE="$SCRIPT_DIR/bin/cache"
ENGINE_CACHE="$CACHE/artifacts/engine"
PKG_CACHE="$CACHE/pkg"
FLUTTER_DIR_NAME="$(basename "$SCRIPT_DIR")"

CONFIGS=(host_debug android_debug_x64 android_debug_arm64 android_release_arm64)

STRIP_DEBUG_SYMBOLS=0
[[ " $* " == *" --strip-debug-symbols "* ]] && STRIP_DEBUG_SYMBOLS=1

# ---------------------------------------------------------------------------
# Step 1: Build engine for all required targets
# ---------------------------------------------------------------------------
echo "=== Tool access ==="
export PATH=$PATH:$SCRIPT_DIR/flutter/engine/src/flutter/bin

DEPOT_TOOLS_DIR="$HOME/.local/depot_tools"

echo "=== Checking depot_tools ==="
if [ ! -d "$DEPOT_TOOLS_DIR" ]; then
  echo "--- depot_tools not found. Cloning into $DEPOT_TOOLS_DIR ---"
  mkdir -p "$(dirname "$DEPOT_TOOLS_DIR")"
  git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git "$DEPOT_TOOLS_DIR"
else
  echo "--- depot_tools found at $DEPOT_TOOLS_DIR ---"
fi

export PATH=$PATH:$DEPOT_TOOLS_DIR

echo "=== Building engine targets ==="
cd "$ENGINE_SRC"

for config in "${CONFIGS[@]}"; do
  echo "--- Configuring $config with gn ---"
  
  # Map the config string to the corresponding gn flags
  case "$config" in
    host_debug)
      python3 ./flutter/tools/gn --runtime-mode debug
      ;;
    android_debug_x64)
      python3 ./flutter/tools/gn --android --android-cpu=x64 --runtime-mode debug
      ;;
    android_debug_arm64)
      python3 ./flutter/tools/gn --android --android-cpu=arm64 --runtime-mode debug
      ;;
    android_release_arm64)
      python3 ./flutter/tools/gn --android --android-cpu=arm64 --runtime-mode release
      ;;
    *)
      echo "Error: Unmapped configuration '$config'"
      exit 1
      ;;
  esac

  echo "--- Building $config with ninja ---"
  # Compile the target using ninja (if you have depot_tools fully configured, you can also use 'autoninja' here for optimal threading)
  ninja -C "out/$config"
done
# ---------------------------------------------------------------------------
# Step 2: Verify build outputs exist
# ---------------------------------------------------------------------------
echo "=== Verifying build outputs ==="
for config in "${CONFIGS[@]}"; do
  if [[ ! -d "$OUT/$config" ]]; then
    echo "ERROR: Build output missing: $OUT/$config"
    exit 1
  fi
done

CRITICAL_FILES=(
  "$OUT/host_debug/gen_snapshot"
  "$OUT/host_debug/flutter_tester"
  "$OUT/host_debug/impellerc"
  "$OUT/host_debug/libflutter_linux_gtk.so"
  "$OUT/host_debug/flutter_patched_sdk/platform_strong.dill"
  "$OUT/host_debug/gen/dart-pkg/sky_engine/lib/ui/ui.dart"
  "$OUT/android_debug_x64/flutter.jar"
  "$OUT/android_debug_arm64/flutter.jar"
  "$OUT/android_release_arm64/flutter.jar"
  "$OUT/android_release_arm64/clang_x64/gen_snapshot"
)
for f in "${CRITICAL_FILES[@]}"; do
  if [[ ! -e "$f" ]]; then
    echo "ERROR: Critical file missing: $f"
    exit 1
  fi
done
echo "All critical files present."

# ---------------------------------------------------------------------------
# Step 3: Strip debug symbols from .so files to reduce package size
# ---------------------------------------------------------------------------
if [[ "$STRIP_DEBUG_SYMBOLS" == "1" ]]; then
  echo "=== Stripping debug symbols from engine binaries ==="
  find "$OUT" -name "*.so" -type f | while read -r sofile; do
    strip --strip-debug "$sofile" 2>/dev/null || true
  done
  # Also strip large executables
  for config in "${CONFIGS[@]}"; do
    for bin in gen_snapshot flutter_tester impellerc font-subset; do
      if [[ -f "$OUT/$config/$bin" ]]; then
        strip --strip-debug "$OUT/$config/$bin" 2>/dev/null || true
      fi
    done
    # gen_snapshot in clang_x64 subdirectory (for android cross-compile)
    if [[ -f "$OUT/$config/clang_x64/gen_snapshot" ]]; then
      strip --strip-debug "$OUT/$config/clang_x64/gen_snapshot" 2>/dev/null || true
    fi
  done
fi

# ---------------------------------------------------------------------------
# Step 4: Prepare cache directories
# ---------------------------------------------------------------------------
echo "=== Preparing cache directories ==="
rm -rf "$ENGINE_CACHE" "$PKG_CACHE"
mkdir -p "$ENGINE_CACHE" "$PKG_CACHE"

# ---------------------------------------------------------------------------
# Step 5: Copy host_debug artifacts → linux-x64/
# ---------------------------------------------------------------------------
echo "=== Copying host artifacts to cache ==="
LINUX_X64="$ENGINE_CACHE/linux-x64"
mkdir -p "$LINUX_X64"

cp "$OUT/host_debug/gen_snapshot"  "$LINUX_X64/"
cp "$OUT/host_debug/flutter_tester" "$LINUX_X64/"
cp "$OUT/host_debug/impellerc"    "$LINUX_X64/"
cp "$OUT/host_debug/font-subset"  "$LINUX_X64/"

cp "$OUT/host_debug/libflutter_linux_gtk.so" "$LINUX_X64/"
cp "$OUT/host_debug/libpath_ops.so"          "$LINUX_X64/"
cp "$OUT/host_debug/libtessellator.so"       "$LINUX_X64/"

cp "$OUT/host_debug/icudtl.dat" "$LINUX_X64/"

cp "$OUT/host_debug/gen/flutter/lib/snapshot/isolate_snapshot.bin"    "$LINUX_X64/"
cp "$OUT/host_debug/gen/flutter/lib/snapshot/vm_isolate_snapshot.bin" "$LINUX_X64/"
cp "$OUT/host_debug/gen/frontend_server_aot.dart.snapshot" "$LINUX_X64/"
cp "$OUT/host_debug/gen/const_finder.dart.snapshot"        "$LINUX_X64/"

cp -r "$OUT/host_debug/flutter_linux" "$LINUX_X64/"
cp -r "$OUT/host_debug/shader_lib"    "$LINUX_X64/"

chmod +x "$LINUX_X64/gen_snapshot" "$LINUX_X64/flutter_tester" \
         "$LINUX_X64/impellerc" "$LINUX_X64/font-subset"

# ---------------------------------------------------------------------------
# Step 6: Copy common artifacts (flutter_patched_sdk)
# ---------------------------------------------------------------------------
echo "=== Copying common artifacts ==="
COMMON="$ENGINE_CACHE/common"
mkdir -p "$COMMON"

cp -r "$OUT/host_debug/flutter_patched_sdk" "$COMMON/flutter_patched_sdk"
cp -r "$OUT/android_release_arm64/flutter_patched_sdk" "$COMMON/flutter_patched_sdk_product"

# ---------------------------------------------------------------------------
# Step 7: Copy android artifacts to cache
# ---------------------------------------------------------------------------
echo "=== Copying Android artifacts to cache ==="

mkdir -p "$ENGINE_CACHE/android-x64"
cp "$OUT/android_debug_x64/flutter.jar" "$ENGINE_CACHE/android-x64/"

mkdir -p "$ENGINE_CACHE/android-arm64"
cp "$OUT/android_debug_arm64/flutter.jar" "$ENGINE_CACHE/android-arm64/"

mkdir -p "$ENGINE_CACHE/android-arm64-release/linux-x64"
cp "$OUT/android_release_arm64/flutter.jar" "$ENGINE_CACHE/android-arm64-release/"
cp "$OUT/android_release_arm64/clang_x64/gen_snapshot" \
   "$ENGINE_CACHE/android-arm64-release/linux-x64/"
chmod +x "$ENGINE_CACHE/android-arm64-release/linux-x64/gen_snapshot"

# ---------------------------------------------------------------------------
# Step 8: Copy packages
# ---------------------------------------------------------------------------
echo "=== Copying packages ==="
cp -r "$OUT/host_debug/gen/dart-pkg/sky_engine" "$PKG_CACHE/sky_engine"
cp -r "$ENGINE_SRC/flutter/lib/gpu"             "$PKG_CACHE/flutter_gpu"

# ---------------------------------------------------------------------------
# Step 9: Create placeholder directories (for stamp system)
#
# Precache checks that ALL expected directories exist. Empty dirs for
# configs we don't build prevent precache from trying to re-download
# (which would fail since our engine hash isn't on GCS).
# ---------------------------------------------------------------------------
echo "=== Creating placeholder directories ==="

PLACEHOLDER_DIRS=(
  # AndroidInternalBuildArtifacts
  android-x86
  android-arm
  android-arm-profile
  android-arm-release
  android-arm64-profile
  android-x64-profile
  android-x64-release
  # AndroidGenSnapshotArtifacts
  android-arm-profile/linux-x64
  android-arm-release/linux-x64
  android-arm64-profile/linux-x64
  android-x64-profile/linux-x64
  android-x64-release/linux-x64
  # LinuxEngineArtifacts
  linux-x64-profile
  linux-x64-release
)
for dir in "${PLACEHOLDER_DIRS[@]}"; do
  mkdir -p "$ENGINE_CACHE/$dir"
done

# ---------------------------------------------------------------------------
# Step 10: Set stamp files
# ---------------------------------------------------------------------------
echo "=== Setting stamp files ==="
STAMP_VALUE=$(cat "$CACHE/engine.stamp")
echo "Engine stamp: $STAMP_VALUE"

for stamp in flutter_sdk android-sdk android-internal-build-artifacts linux-sdk; do
  printf '%s' "$STAMP_VALUE" > "$CACHE/${stamp}.stamp"
done

# ---------------------------------------------------------------------------
# Step 11: Create and commit bin/internal/engine.version
#
# update_engine_version.sh checks if this file is tracked in git.
# If found, it uses the content directly — bypassing content_aware_hash.sh
# which does heavy git operations that may produce wrong hashes on forks.
# ---------------------------------------------------------------------------
echo "=== Creating engine.version ==="
printf '%s' "$STAMP_VALUE" > "$SCRIPT_DIR/bin/internal/engine.version"

cd "$SCRIPT_DIR"
#git add bin/internal/engine.version
#git commit -m "Pin engine.version for SDK distribution"

# ---------------------------------------------------------------------------
# Step 12: Create version file
# ---------------------------------------------------------------------------
echo "=== Creating version file ==="
printf '%s' "$VERSION_STRING" > "$SCRIPT_DIR/version"

# ---------------------------------------------------------------------------
# Step 13: Create tar.gz
#
# Includes:
#   - SDK source (packages/, bin/ scripts)
#   - .git/ (required by wrapper scripts)
#   - Engine build outputs in engine/src/out/ (for --local-engine)
#   - Cache artifacts + stamps (for precache)
#   - flutter.version.json (for version detection)
#
# Excludes from engine/src/out/:
#   - obj/, lib.stripped/, exe.unstripped/ (build intermediates)
#   - build.ninja, compile_commands.json, toolchain.ninja (build metadata)
#   - *.ninja, *.ninja.d, *.ninja.stamp (ninja files)
#   - *.tmp, *.TOC (temp files)
#   - gn_logs.txt, gn_trace.json (GN logs)
#   - dart-sdk/ (downloaded by precache into bin/cache/)
#   - zip_archives/ (CI-only)
# ---------------------------------------------------------------------------
echo "=== Creating flutter_sdk_linux.tar.gz ==="
cd "$SCRIPT_DIR/.."

tar -I pigz -cf "$SCRIPT_DIR/flutter_sdk_linux.tar.gz" \
  --exclude='*/obj/*' \
  --exclude='*/lib.stripped/*' \
  --exclude='*/exe.unstripped/*' \
  --exclude='build.ninja' \
  --exclude='build.ninja.d' \
  --exclude='build.ninja.stamp' \
  --exclude='toolchain.ninja' \
  --exclude='compile_commands.json' \
  --exclude='*.tmp' \
  --exclude='*.TOC' \
  --exclude='gn_logs.txt' \
  --exclude='gn_trace.json' \
  --exclude='*/zip_archives' \
  "$FLUTTER_DIR_NAME/packages/" \
  "$FLUTTER_DIR_NAME/bin/flutter" \
  "$FLUTTER_DIR_NAME/bin/dart" \
  "$FLUTTER_DIR_NAME/bin/flutter-dev" \
  "$FLUTTER_DIR_NAME/bin/internal/" \
  "$FLUTTER_DIR_NAME/bin/cache/artifacts/engine/" \
  "$FLUTTER_DIR_NAME/bin/cache/pkg/" \
  "$FLUTTER_DIR_NAME/bin/cache/engine.stamp" \
  "$FLUTTER_DIR_NAME/bin/cache/flutter_sdk.stamp" \
  "$FLUTTER_DIR_NAME/bin/cache/android-sdk.stamp" \
  "$FLUTTER_DIR_NAME/bin/cache/android-internal-build-artifacts.stamp" \
  "$FLUTTER_DIR_NAME/bin/cache/linux-sdk.stamp" \
  "$FLUTTER_DIR_NAME/bin/cache/flutter.version.json" \
  "$FLUTTER_DIR_NAME/.git/" \
  "$FLUTTER_DIR_NAME/engine/src/out/host_debug/" \
  "$FLUTTER_DIR_NAME/engine/src/out/android_debug_x64/" \
  "$FLUTTER_DIR_NAME/engine/src/out/android_debug_arm64/" \
  "$FLUTTER_DIR_NAME/engine/src/out/android_release_arm64/" \
  "$FLUTTER_DIR_NAME/engine/src/flutter/prebuilts/linux-x64/esbuild/" \
  "$FLUTTER_DIR_NAME/engine/src/flutter/prebuilts/linux-x64/dart-sdk/" \
  "$FLUTTER_DIR_NAME/LICENSE" \
  "$FLUTTER_DIR_NAME/README.md" \
  "$FLUTTER_DIR_NAME/pubspec.yaml" \
  "$FLUTTER_DIR_NAME/pubspec.lock" \
  "$FLUTTER_DIR_NAME/analysis_options.yaml" \
  "$FLUTTER_DIR_NAME/version" \
  "$FLUTTER_DIR_NAME/PATENT_GRANT"

echo "=== File archived. Calculating SHA1 ==="
SIZE=$(du -h "$SCRIPT_DIR/flutter_sdk_linux.tar.gz" | cut -f1)
ARTIFACT_HASH=$(sha1sum -b $SCRIPT_DIR/flutter_sdk_linux.tar.gz | cut -d ' ' -f1)
FINAL_ARTIFACT_FILE=flutter_sdk_linux-$ARTIFACT_HASH.tar.gz
mv $SCRIPT_DIR/flutter_sdk_linux.tar.gz $SCRIPT_DIR/$FINAL_ARTIFACT_FILE

echo ""
echo "=== Done ==="
echo "Package: $SCRIPT_DIR/$FINAL_ARTIFACT_FILE ($SIZE)"
echo "Version: $VERSION_STRING"
echo ""
echo "To use in devcontainer:"
echo "  1. Extract:  tar xzf $FINAL_ARTIFACT_FILE"
echo "  2. Fix git:  git config --global --add safe.directory /path/to/$FLUTTER_DIR_NAME"
echo "  3. Precache: $FLUTTER_DIR_NAME/bin/flutter precache"
echo "  4. Run:      flutter run --local-engine-host=host_debug --local-engine=android_debug_arm64"
