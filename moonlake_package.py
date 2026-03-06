#!/usr/bin/env python3
"""
package_for_distribution.py

Builds the Flutter engine for required targets and packages the SDK into
a distributable tarball for app developers.

Usage examples:
    # Build defaults (host_debug + android targets), package with pigz:
    python3 package_for_distribution.py

    # Specific targets, strip symbols:
    python3 package_for_distribution.py \
        --configs host_debug android_release_arm64 \
        --strip-debug-symbols

    # Full unoptimized build for profiling:
    python3 package_for_distribution.py \
        --configs host_debug_unopt android_debug_unopt_arm64

    # Custom output name and compression:
    python3 package_for_distribution.py \
        --version 3.43.0-custom \
        --compression-program gzip \
        --compression-level 6

After extracting, developers must:
    1. git config --global --add safe.directory <path-to-flutter>
    2. python3 post_sdk_unpack.py --sdk-dir <path-to-flutter>
    3. flutter precache  (fetches dart-sdk, web-sdk, fonts, gradle)
    4. flutter run --local-engine-host=host_debug --local-engine=<target>
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)-5s] %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

logger = logging.getLogger("flutter-packager")


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity >= 1:
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.setLevel(level)
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Constants & enums
# ---------------------------------------------------------------------------
DEFAULT_VERSION = "3.42.0-moonlake"
DEFAULT_CONFIGS = ["host_debug", "android_debug_x64", "android_debug_arm64", "android_release_arm64"]
DEPOT_TOOLS_REPO = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"


class RuntimeMode(Enum):
    DEBUG = "debug"
    PROFILE = "profile"
    RELEASE = "release"


class AndroidCpu(Enum):
    ARM = "arm"
    ARM64 = "arm64"
    X64 = "x64"
    X86 = "x86"


# ---------------------------------------------------------------------------
# Build target parsing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BuildTarget:
    """Parsed representation of a build configuration string."""
    name: str               # original config string, e.g. "android_debug_arm64"
    is_android: bool
    is_host: bool
    runtime_mode: RuntimeMode
    android_cpu: Optional[AndroidCpu]
    unopt: bool

    @classmethod
    def parse(cls, config: str) -> "BuildTarget":
        """Parse a config string like 'host_debug', 'android_release_arm64', 'host_debug_unopt'."""
        original = config
        parts = config.lower().split("_")

        if len(parts) < 2:
            raise ValueError(
                f"Invalid config '{original}': expected at least <platform>_<mode> "
                f"(e.g. host_debug, android_release_arm64)"
            )

        # Platform
        plat = parts[0]
        if plat not in ("host", "android"):
            raise ValueError(
                f"Invalid config '{original}': platform must be 'host' or 'android', got '{plat}'"
            )
        is_host = plat == "host"
        is_android = plat == "android"

        # Runtime mode
        mode_str = parts[1]
        try:
            runtime_mode = RuntimeMode(mode_str)
        except ValueError:
            raise ValueError(
                f"Invalid config '{original}': runtime mode must be one of "
                f"{[m.value for m in RuntimeMode]}, got '{mode_str}'"
            )

        # Remaining parts: unopt flag and/or android cpu
        remaining = parts[2:]
        unopt = False
        android_cpu: Optional[AndroidCpu] = None

        if "unopt" in remaining:
            unopt = True
            remaining = [r for r in remaining if r != "unopt"]

        if is_android:
            if not remaining:
                raise ValueError(
                    f"Invalid config '{original}': android targets require a CPU suffix "
                    f"(arm, arm64, x64, x86)"
                )
            cpu_str = remaining[0]
            remaining = remaining[1:]
            try:
                android_cpu = AndroidCpu(cpu_str)
            except ValueError:
                raise ValueError(
                    f"Invalid config '{original}': unknown android CPU '{cpu_str}', "
                    f"expected one of {[c.value for c in AndroidCpu]}"
                )
        elif is_host and remaining:
            raise ValueError(
                f"Invalid config '{original}': host targets do not accept CPU suffixes. "
                f"Unexpected tokens: {remaining}"
            )

        if remaining:
            raise ValueError(f"Invalid config '{original}': unexpected tokens {remaining}")

        return cls(
            name=original,
            is_android=is_android,
            is_host=is_host,
            runtime_mode=runtime_mode,
            android_cpu=android_cpu,
            unopt=unopt,
        )

    def gn_args(self) -> list[str]:
        """Return the gn arguments for this target."""
        args: list[str] = []
        if self.is_android:
            args.append("--android")
            args.append(f"--android-cpu={self.android_cpu.value}")
        args.append(f"--runtime-mode={self.runtime_mode.value}")
        if self.unopt:
            args.append("--unoptimized")
        return args

    def out_dir_name(self) -> str:
        """
        Derive the ninja output directory name that gn will produce.

        Convention used by Flutter's gn:
            host_debug         -> host_debug
            host_debug_unopt   -> host_debug_unopt
            android_debug_arm64 -> android_debug_arm64
            android_debug_unopt_arm64 -> android_debug_unopt_arm64
        """
        return self.name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(
    cmd: Sequence[str | Path],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess with logging."""
    cmd_str = " ".join(str(c) for c in cmd)
    logger.debug("Running: %s (cwd=%s)", cmd_str, cwd or ".")

    merged_env = {**os.environ, **(env or {})}
    try:
        result = subprocess.run(
            [str(c) for c in cmd],
            cwd=cwd,
            env=merged_env,
            check=check,
            capture_output=capture,
            text=capture,
        )
        return result
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        raise
    except subprocess.CalledProcessError as exc:
        logger.error("Command failed (exit %d): %s", exc.returncode, cmd_str)
        if capture and exc.stderr:
            for line in exc.stderr.strip().splitlines()[-20:]:
                logger.error("  stderr: %s", line)
        raise


def _sha1_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute SHA-1 hex digest of a file."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TiB"


def _detect_compression_program() -> str:
    """
    Choose the fastest available compressor.

    Priority: pigz > gzip (pigz is a parallel gzip implementation and
    typically 4-8x faster on multi-core machines).
    """
    for prog in ("pigz", "gzip"):
        if shutil.which(prog):
            logger.info("Selected compression program: %s", prog)
            return prog

    logger.warning("No gzip-compatible compressor found; falling back to Python gzip (slow)")
    return "__python__"


# ---------------------------------------------------------------------------
# Core packager
# ---------------------------------------------------------------------------
@dataclass
class PackagerConfig:
    """All resolved configuration for a packaging run."""
    version: str
    script_dir: Path
    engine_src: Path
    flutter_dir_name: str
    configs: list[BuildTarget]
    strip_debug_symbols: bool
    depot_tools_dir: Path
    compression_program: str
    compression_level: int
    compression_threads: int
    ninja_program: str
    skip_build: bool
    dry_run: bool

    # Derived paths
    @property
    def out_dir(self) -> Path:
        return self.engine_src / "out"

    @property
    def cache_dir(self) -> Path:
        return self.script_dir / "bin" / "cache"

    @property
    def host_targets(self) -> list[BuildTarget]:
        return [c for c in self.configs if c.is_host]

    @property
    def android_targets(self) -> list[BuildTarget]:
        return [c for c in self.configs if c.is_android]


class Packager:
    """Orchestrates the build, validation, caching, and packaging steps."""

    def __init__(self, cfg: PackagerConfig):
        self.cfg = cfg
        self._step = 0
        self._timings: list[tuple[str, float]] = []

    # -- public entry point --------------------------------------------------

    def run(self) -> Path:
        """
        Execute all packaging steps. Returns the path to the final artifact.

        The Flutter SDK has a layered architecture that this pipeline mirrors:

        1. Toolchain setup (depot_tools) — Chromium's build toolchain provides
           gn (meta-build generator) and ninja (build executor), which are the
           standard entry points for compiling the Flutter engine.

        2. Engine compilation — The engine (C++/Dart) is compiled per-target.
           Each target produces an output directory under engine/src/out/ whose
           name encodes platform, mode, and CPU (e.g. android_release_arm64).

        3. Stamp alignment — Flutter uses .stamp files to detect whether its
           cache is current. For custom engine builds (not on Google's CDN),
           stamps must be set manually to prevent the CLI from attempting
           (and failing) to download official artifacts.

        4. Tarball creation — The archive includes the raw build outputs
           (for --local-engine), aligned stamps, and the .git directory
           which Flutter's wrapper scripts need for version detection.

        Cache population (copying artifacts from engine/src/out/ into
        bin/cache/artifacts/engine/) is handled separately by
        post_sdk_unpack.py, which developers run after extracting the tarball.
        This avoids packaging the same files twice.
        """
        total_start = time.monotonic()

        self._ensure_depot_tools()
        self._build_targets()
        self._verify_outputs()
        self._strip_symbols()
        self._set_stamps()
        self._create_engine_version()
        self._create_version_file()
        artifact = self._create_tarball()

        total_elapsed = time.monotonic() - total_start
        self._print_summary(artifact, total_elapsed)
        return artifact

    # -- step helpers --------------------------------------------------------

    def _step_banner(self, title: str) -> float:
        self._step += 1
        logger.info("=== Step %d: %s ===", self._step, title)
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  Step {self._step}: {title}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        return time.monotonic()

    def _step_done(self, title: str, start: float) -> None:
        elapsed = time.monotonic() - start
        self._timings.append((title, elapsed))
        logger.info("  Step completed in %.1fs", elapsed)

    # -- steps ---------------------------------------------------------------

    def _ensure_depot_tools(self) -> None:
        """
        Ensure Chromium's depot_tools are available on PATH.

        The Flutter engine is built with Chromium's build toolchain: gn generates
        ninja build files, and ninja (or autoninja) executes the actual compilation.
        Both are distributed via depot_tools — a git repository that also provides
        gclient, cipd, and other infrastructure the engine's DEPS file relies on.

        We also add the engine's own bin/ directory to PATH, which contains helper
        scripts used during the build (e.g. Dart SDK binaries bundled with the
        engine source).
        """
        t = self._step_banner("Ensure depot_tools")
        dt = self.cfg.depot_tools_dir

        if dt.is_dir():
            logger.info("depot_tools found at %s", dt)
        else:
            logger.info("Cloning depot_tools into %s", dt)
            if self.cfg.dry_run:
                logger.info("[DRY RUN] Would clone depot_tools")
            else:
                dt.parent.mkdir(parents=True, exist_ok=True)
                _run(["git", "clone", DEPOT_TOOLS_REPO, str(dt)])

        # Ensure PATH contains depot_tools and flutter engine bin
        extra = str(dt)
        engine_bin = self.cfg.script_dir / "flutter" / "engine" / "src" / "flutter" / "bin"
        if engine_bin.is_dir():
            extra = f"{extra}:{engine_bin}"
        os.environ["PATH"] = f"{os.environ.get('PATH', '')}:{extra}"
        logger.debug("PATH updated: ...:%s", extra)
        self._step_done("Ensure depot_tools", t)

    def _build_targets(self) -> None:
        """
        Compile the Flutter engine for each requested target configuration.

        The engine build is a two-phase process per target:

        1. gn (Generate Ninja) — flutter/tools/gn is a Python wrapper around
           Chromium's gn that translates high-level flags (--android, --android-cpu,
           --runtime-mode, --unoptimized) into a full set of GN args. It writes
           build.ninja into engine/src/out/<target_name>/.

        2. ninja — Executes the generated build graph. autoninja (from depot_tools)
           is preferred because it auto-tunes parallelism based on available RAM and
           CPU, which matters for the engine's large C++ codebase (Skia, Impeller,
           Dart VM, ICU, etc.).

        Each target name (e.g. "android_release_arm64") is also the output directory
        name under engine/src/out/. This naming convention is how Flutter's
        --local-engine flag resolves the correct build artifacts at runtime.
        """
        t = self._step_banner("Build engine targets")

        if self.cfg.skip_build:
            logger.info("Skipping build (--skip-build)")
            self._step_done("Build engine targets (skipped)", t)
            return

        for target in self.cfg.configs:
            self._build_one(target)

        self._step_done("Build engine targets", t)

    def _build_one(self, target: BuildTarget) -> None:
        logger.info("--- Configuring %s ---", target.name)
        gn_cmd = ["python3", "./flutter/tools/gn"] + target.gn_args()

        if self.cfg.dry_run:
            logger.info("[DRY RUN] Would run: %s", " ".join(gn_cmd))
            logger.info("[DRY RUN] Would run: %s -C out/%s",
                        self.cfg.ninja_program, target.out_dir_name())
            return

        _run(gn_cmd, cwd=self.cfg.engine_src)

        logger.info("--- Building %s ---", target.name)

        # autoninja from depot_tools is preferred for its automatic -j tuning,
        # but recent versions unconditionally try to start Chromium's Android
        # build server (fast_local_dev_server.py) which doesn't exist in
        # Flutter engine checkouts.  Setting ANTHROPIC_NO_ANDROID_BUILD_SERVER
        # or CAUTION_SIBLING_BUILDDIR can suppress this in some depot_tools
        # versions, but the most reliable workaround for Flutter is to set
        # ANDROID_BUILD_SERVER_DISABLED=1, or fall back to plain ninja.
        build_env: dict[str, str] = {"ANDROID_BUILD_SERVER_DISABLED": "1"}

        _run(
            [self.cfg.ninja_program, "-C", f"out/{target.out_dir_name()}"],
            cwd=self.cfg.engine_src,
            env=build_env,
        )

    def _verify_outputs(self) -> None:
        """
        Verify that all expected build artifacts exist before packaging.

        A successful host build produces several categories of artifacts:
          - gen_snapshot: the Dart AOT compiler, used to compile Dart to native code.
          - flutter_tester: headless test runner for Flutter widget tests on the host.
          - impellerc: Impeller's shader compiler (the modern rendering backend).
          - libflutter_linux_gtk.so: the engine's embedder library for Linux/GTK.
          - flutter_patched_sdk/: a modified Dart SDK with Flutter-specific platform
            libraries (platform_strong.dill is the kernel representation).
          - sky_engine (gen/dart-pkg/sky_engine/): Dart package exposing dart:ui,
            the low-level interface between framework and engine.

        A successful android build produces:
          - flutter.jar: the engine packaged as an Android AAR/JAR for Gradle.
          - clang_x64/gen_snapshot (release/profile only): a cross-compiled AOT
            snapshot compiler that runs on x64 Linux but emits arm64 machine code.
            Debug builds don't need this because they use JIT via the Dart VM.

        All errors are collected before failing, so a single run surfaces every
        missing artifact rather than stopping at the first.
        """
        t = self._step_banner("Verify build outputs")
        errors: list[str] = []

        for target in self.cfg.configs:
            out = self.cfg.out_dir / target.out_dir_name()
            if not out.is_dir():
                errors.append(f"Build output directory missing: {out}")

        # Critical files for host targets
        for ht in self.cfg.host_targets:
            base = self.cfg.out_dir / ht.out_dir_name()
            host_critical = [
                "gen_snapshot",
                "flutter_tester",
                "impellerc",
                "libflutter_linux_gtk.so",
                "flutter_patched_sdk/platform_strong.dill",
                "gen/dart-pkg/sky_engine/lib/ui/ui.dart",
            ]
            for rel in host_critical:
                p = base / rel
                if not p.exists():
                    errors.append(f"Critical host file missing: {p}")

        # Critical files for android targets
        for at in self.cfg.android_targets:
            base = self.cfg.out_dir / at.out_dir_name()
            jar = base / "flutter.jar"
            if not jar.exists():
                errors.append(f"Critical android file missing: {jar}")

            if at.runtime_mode in (RuntimeMode.RELEASE, RuntimeMode.PROFILE):
                gs = base / "clang_x64" / "gen_snapshot"
                if not gs.exists():
                    errors.append(f"Critical gen_snapshot missing: {gs}")

        if errors:
            for e in errors:
                logger.error(e)
            raise RuntimeError(
                f"Verification failed with {len(errors)} error(s). "
                "Ensure all targets built successfully."
            )

        logger.info("All critical files present.")
        self._step_done("Verify build outputs", t)

    def _strip_symbols(self) -> None:
        """
        Optionally strip debug symbols from engine binaries to reduce archive size.

        The engine build produces unstripped binaries with full DWARF debug info.
        This is essential for local debugging but adds hundreds of megabytes to
        the final package. Stripping removes debug sections while preserving the
        symbol table needed for dynamic linking.

        Targets:
          - All .so shared libraries (libflutter_linux_gtk.so, Impeller libs, etc.)
          - Host executables: gen_snapshot, flutter_tester, impellerc, font-subset
          - Cross-compiled gen_snapshot under clang_x64/ (used for AOT compilation
            targeting Android from a Linux host)

        Uses --strip-debug (not --strip-all) to keep the dynamic symbol table
        intact — fully stripped .so files would fail to load at runtime.
        """
        t = self._step_banner("Strip debug symbols")

        if not self.cfg.strip_debug_symbols:
            logger.info("Skipping (--strip-debug-symbols not set)")
            self._step_done("Strip debug symbols (skipped)", t)
            return

        strip_bin = shutil.which("strip")
        if not strip_bin:
            logger.warning("'strip' not found in PATH; skipping symbol stripping")
            self._step_done("Strip debug symbols (strip not found)", t)
            return

        count = 0
        out = self.cfg.out_dir

        # Strip .so files
        for so_file in out.rglob("*.so"):
            if so_file.is_file():
                _run(["strip", "--strip-debug", str(so_file)], check=False)
                count += 1

        # Strip known executables
        exe_names = ["gen_snapshot", "flutter_tester", "impellerc", "font-subset"]
        for target in self.cfg.configs:
            target_dir = out / target.out_dir_name()
            for exe in exe_names:
                p = target_dir / exe
                if p.is_file():
                    _run(["strip", "--strip-debug", str(p)], check=False)
                    count += 1
            # Cross-compile gen_snapshot
            gs = target_dir / "clang_x64" / "gen_snapshot"
            if gs.is_file():
                _run(["strip", "--strip-debug", str(gs)], check=False)
                count += 1

        logger.info("Stripped %d files.", count)
        self._step_done("Strip debug symbols", t)

    def _set_stamps(self) -> None:
        """
        Align all cache stamp files to the current engine revision.

        Flutter's artifact caching system uses .stamp files in bin/cache/ to
        track which engine revision each artifact group was fetched for. On
        every `flutter` invocation, the wrapper script compares the stamp value
        against the expected engine hash. A mismatch triggers a re-download.

        The authoritative stamp is engine.stamp, written during `gclient sync`.
        We propagate its value to the per-group stamps:
          - flutter_sdk.stamp: Dart SDK and core Flutter tools
          - android-sdk.stamp: Android engine artifacts (flutter.jar etc.)
          - android-internal-build-artifacts.stamp: internal android tooling
          - linux-sdk.stamp: Linux desktop engine artifacts

        Without this alignment, the first `flutter` command after extracting
        the SDK would detect a stamp mismatch and try to fetch from GCS,
        which fails for custom engine builds.
        """
        t = self._step_banner("Set stamp files")

        engine_stamp_file = self.cfg.cache_dir / "engine.stamp"
        if not engine_stamp_file.exists():
            raise FileNotFoundError(
                f"Engine stamp file not found: {engine_stamp_file}\n"
                "This file is required to align cache stamps. "
                "Ensure you're running from a valid Flutter SDK checkout."
            )

        stamp_value = engine_stamp_file.read_text().strip()
        logger.info("Engine stamp: %s", stamp_value)

        stamp_names = [
            "flutter_sdk.stamp",
            "android-sdk.stamp",
            "android-internal-build-artifacts.stamp",
            "linux-sdk.stamp",
        ]
        for name in stamp_names:
            p = self.cfg.cache_dir / name
            p.write_text(stamp_value)
            logger.debug("Wrote stamp %s", p)

        self._step_done("Set stamp files", t)

    def _create_engine_version(self) -> None:
        """
        Pin the engine version to bypass content-aware hashing.

        Flutter determines the expected engine hash via two mechanisms:
          1. bin/internal/engine.version — if this file exists and is tracked
             in git, its content is used directly as the engine hash.
          2. content_aware_hash.sh — if engine.version is absent, this script
             computes a hash from the engine source tree using git operations.

        Mechanism (2) is fragile on forks and shallow clones because it relies
        on specific git history. By writing engine.version with our engine.stamp
        value, we force mechanism (1), ensuring the SDK consistently resolves
        our custom-built engine regardless of git state.
        """
        t = self._step_banner("Create engine.version")

        stamp_value = (self.cfg.cache_dir / "engine.stamp").read_text().strip()
        ev = self.cfg.script_dir / "bin" / "internal" / "engine.version"
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(stamp_value)
        logger.info("Wrote engine.version: %s", ev)

        self._step_done("Create engine.version", t)

    def _create_version_file(self) -> None:
        """
        Write the human-readable SDK version string.

        The top-level `version` file (e.g. "3.42.0-moonlake") is read by
        `flutter --version` and embedded in app metadata. For custom SDK
        distributions this is typically a fork identifier that distinguishes
        it from upstream Flutter releases.
        """
        t = self._step_banner("Create version file")

        vf = self.cfg.script_dir / "version"
        vf.write_text(self.cfg.version)
        logger.info("Wrote version '%s' to %s", self.cfg.version, vf)

        self._step_done("Create version file", t)

    def _create_tarball(self) -> Path:
        """
        Create the distributable tar.gz archive and rename with SHA-1.

        The archive includes three categories of content:

        1. SDK framework & tooling (used by `flutter` CLI):
           - packages/: Flutter framework Dart source (widgets, material, etc.)
           - bin/flutter, bin/dart: wrapper shell scripts
           - bin/internal/: version files, update scripts
           - bin/cache/*.stamp + flutter.version.json: stamp files that prevent
             the Flutter CLI from re-downloading artifacts from GCS

        2. Engine build outputs (used by --local-engine and post_sdk_unpack.py):
           - engine/src/out/<target>/: raw ninja build output per target.
             post_sdk_unpack.py reads from here to populate bin/cache/.
           - engine/src/flutter/prebuilts/: Dart SDK and esbuild binaries
             used by the engine's own build system.

        3. Git & metadata (required by Flutter internals):
           - .git/: Flutter's wrapper scripts call `git rev-parse` and
             `git describe` for version detection. Without .git/, the
             `flutter` command fails at startup.
           - LICENSE, README.md, pubspec.yaml, etc.

        Note: bin/cache/artifacts/engine/ and bin/cache/pkg/ are NOT included.
        These are populated by post_sdk_unpack.py after extraction, avoiding
        the duplication of files already present under engine/src/out/.
        """
        t = self._step_banner("Create tarball")

        fdn = self.cfg.flutter_dir_name
        parent_dir = self.cfg.script_dir.parent

        # --- Build the list of paths to include ---
        include_paths = [
            f"{fdn}/packages/",
            f"{fdn}/bin/flutter",
            f"{fdn}/bin/dart",
            f"{fdn}/bin/flutter-dev",
            f"{fdn}/bin/internal/",
            # Stamp files only — cache artifacts are populated by post_sdk_unpack.py
            f"{fdn}/bin/cache/engine.stamp",
            f"{fdn}/bin/cache/flutter_sdk.stamp",
            f"{fdn}/bin/cache/android-sdk.stamp",
            f"{fdn}/bin/cache/android-internal-build-artifacts.stamp",
            f"{fdn}/bin/cache/linux-sdk.stamp",
            f"{fdn}/bin/cache/flutter.version.json",
            f"{fdn}/.git/",
        ]

        # Engine out dirs: only include targets that were built
        for target in self.cfg.configs:
            include_paths.append(f"{fdn}/engine/src/out/{target.out_dir_name()}/")

        # Prebuilt tools
        prebuilt_dirs = [
            f"{fdn}/engine/src/flutter/prebuilts/linux-x64/esbuild/",
            f"{fdn}/engine/src/flutter/prebuilts/linux-x64/dart-sdk/",
        ]
        for pd in prebuilt_dirs:
            full = parent_dir / pd
            if full.exists():
                include_paths.append(pd)

        # Custom flutter_gpu lib
        include_paths.append(f"{fdn}/engine/src/flutter/lib/gpu")

        # Top-level files
        for fname in ("LICENSE", "README.md", "pubspec.yaml", "pubspec.lock",
                       "analysis_options.yaml", "version", "PATENT_GRANT"):
            fpath = parent_dir / fdn / fname
            if fpath.exists():
                include_paths.append(f"{fdn}/{fname}")
            else:
                logger.debug("Optional top-level file not found (skipping from archive): %s", fname)

        # --- Exclude patterns (build intermediates) ---
        excludes = [
            "*/obj/*",
            "*/lib.stripped/*",
            "*/exe.unstripped/*",
            "build.ninja",
            "build.ninja.d",
            "build.ninja.stamp",
            "toolchain.ninja",
            "compile_commands.json",
            "*.tmp",
            "*.TOC",
            "gn_logs.txt",
            "gn_trace.json",
            "*/zip_archives",
        ]

        # --- Choose compression approach ---
        # Optimization: use pigz (parallel gzip) when available, or
        # use an external compressor with configurable level and threads.
        #
        # pigz with -p<N> uses N threads, dramatically reducing wall time
        # on multi-core build machines (typically 4-8x faster than gzip).
        # For even faster compression (at cost of ~5% larger output), use
        # --compression-level 1.

        comp = self.cfg.compression_program
        level = self.cfg.compression_level
        threads = self.cfg.compression_threads

        tmp_tar = self.cfg.script_dir / "flutter_sdk_linux.tar.gz"

        tar_cmd: list[str] = ["tar"]

        if comp == "__python__":
            # Fall back to tar's built-in gzip
            tar_cmd.append("czf")
            tar_cmd.append(str(tmp_tar))
        else:
            # Use external compressor (pigz, gzip, zstd, etc.)
            comp_args = comp
            if comp == "pigz":
                comp_args = f"pigz -p{threads} -{level}"
            elif comp == "gzip":
                comp_args = f"gzip -{level}"
            elif comp == "zstd":
                comp_args = f"zstd -T{threads} -{level}"

            tar_cmd.extend([
                f"--use-compress-program={comp_args}",
                "-cf",
                str(tmp_tar),
            ])

        for pat in excludes:
            tar_cmd.extend(["--exclude", pat])

        tar_cmd.extend(include_paths)

        if self.cfg.dry_run:
            logger.info("[DRY RUN] Would create tarball with %d include paths", len(include_paths))
            logger.info("[DRY RUN] Command: %s", " ".join(tar_cmd))
            self._step_done("Create tarball (dry run)", t)
            return tmp_tar

        _run(tar_cmd, cwd=parent_dir)

        # --- Compute hash and rename ---
        logger.info("Computing SHA-1...")
        sha1 = _sha1_file(tmp_tar)
        final_name = f"flutter_sdk_linux-{sha1}.tar.gz"
        final_path = self.cfg.script_dir / final_name
        tmp_tar.rename(final_path)

        size = final_path.stat().st_size
        logger.info("Artifact: %s (%s)", final_path, _human_size(size))

        self._step_done("Create tarball", t)
        return final_path

    def _print_summary(self, artifact: Path, total_elapsed: float) -> None:
        """Print a final summary with timings."""
        size_str = _human_size(artifact.stat().st_size) if artifact.exists() else "N/A"

        print("\n" + "=" * 60, file=sys.stderr)
        print("  PACKAGING COMPLETE", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"  Artifact : {artifact}", file=sys.stderr)
        print(f"  Size     : {size_str}", file=sys.stderr)
        print(f"  Version  : {self.cfg.version}", file=sys.stderr)
        print(f"  Targets  : {', '.join(t.name for t in self.cfg.configs)}", file=sys.stderr)
        print(f"  Total    : {total_elapsed:.1f}s", file=sys.stderr)
        print(file=sys.stderr)

        if self._timings:
            print("  Step Timings:", file=sys.stderr)
            for name, elapsed in self._timings:
                print(f"    {name:.<45s} {elapsed:6.1f}s", file=sys.stderr)
            print(file=sys.stderr)

        print("  After extracting:", file=sys.stderr)
        print(f"    1. Extract:  tar xzf {artifact.name}", file=sys.stderr)
        print(f"    2. Fix git:  git config --global --add safe.directory "
              f"/path/to/{self.cfg.flutter_dir_name}", file=sys.stderr)
        print(f"    3. Setup:    python3 post_sdk_unpack.py "
              f"--sdk-dir /path/to/{self.cfg.flutter_dir_name}", file=sys.stderr)
        print(f"    4. Precache: {self.cfg.flutter_dir_name}/bin/flutter precache", file=sys.stderr)
        print(f"    5. Run:      flutter run --local-engine-host=host_debug "
              f"--local-engine=<target>", file=sys.stderr)
        print(file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_script_dir(arg: Optional[str]) -> Path:
    """Resolve --script-dir or auto-detect from CWD / script location."""
    if arg:
        p = Path(arg).resolve()
        if not p.is_dir():
            raise ValueError(f"--script-dir does not exist: {p}")
        return p

    # Try CWD first, then fall back to script's own directory
    cwd = Path.cwd()
    if (cwd / "bin" / "flutter").exists():
        return cwd

    script_path = Path(__file__).resolve().parent
    if (script_path / "bin" / "flutter").exists():
        return script_path

    raise ValueError(
        "Cannot auto-detect Flutter SDK root. "
        "Run from the SDK directory or pass --script-dir explicitly."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build and package Flutter SDK for distribution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Supported config format: <platform>_<mode>[_unopt][_<cpu>]
              Platforms : host, android
              Modes     : debug, profile, release
              CPU (android only): arm, arm64, x64, x86

            Examples:
              host_debug                 Host debug build
              host_debug_unopt           Host debug unoptimized
              android_debug_arm64        Android debug for arm64
              android_release_arm64      Android release for arm64
              android_profile_x64        Android profile for x64
              android_debug_unopt_arm64  Android debug unoptimized for arm64
        """),
    )

    p.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"Version string embedded in the SDK (default: {DEFAULT_VERSION})",
    )
    p.add_argument(
        "--script-dir",
        default=None,
        help="Path to the Flutter SDK root (auto-detected if omitted)",
    )
    p.add_argument(
        "--engine-src",
        default=None,
        help="Path to engine/src (default: <script-dir>/engine/src)",
    )
    p.add_argument(
        "--flutter-dir-name",
        default=None,
        help="Name of the Flutter directory for the tarball (default: basename of script-dir)",
    )
    p.add_argument(
        "--configs",
        nargs="+",
        default=DEFAULT_CONFIGS,
        metavar="TARGET",
        help=(
            "Build targets to compile and package "
            f"(default: {' '.join(DEFAULT_CONFIGS)})"
        ),
    )
    p.add_argument(
        "--strip-debug-symbols",
        action="store_true",
        default=False,
        help="Strip debug symbols from .so files and executables to reduce size",
    )
    p.add_argument(
        "--depot-tools-dir",
        default=None,
        help="Path to depot_tools (default: ~/.local/depot_tools)",
    )
    p.add_argument(
        "--ninja",
        default=None,
        choices=["ninja", "autoninja"],
        help=(
            "Ninja binary to use for builds (default: auto-detect). "
            "autoninja auto-tunes parallelism but may fail in Flutter engine "
            "checkouts due to Chromium's Android build server dependency. "
            "Use --ninja=ninja to bypass that issue."
        ),
    )

    # Compression options
    comp = p.add_argument_group("compression")
    comp.add_argument(
        "--compression-program",
        default=None,
        choices=["pigz", "gzip", "zstd"],
        help=(
            "External compressor to use (default: auto-detect, prefers pigz). "
            "pigz uses parallel threads and is 4-8x faster than gzip."
        ),
    )
    comp.add_argument(
        "--compression-level",
        type=int,
        default=6,
        choices=range(1, 20),
        metavar="1-19",
        help="Compression level (default: 6). Lower = faster, larger output.",
    )
    comp.add_argument(
        "--compression-threads",
        type=int,
        default=0,
        metavar="N",
        help="Threads for parallel compressors like pigz (default: 0 = all cores)",
    )

    # Workflow options
    p.add_argument(
        "--skip-build",
        action="store_true",
        default=False,
        help="Skip the gn/ninja build step (use existing build outputs)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be done without executing builds or creating the tarball",
    )
    p.add_argument(
        "-v", "--verbose",
        action="count",
        default=1,
        help="Increase verbosity (-v info, -vv debug)",
    )
    p.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Suppress all output except errors",
    )

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    verbosity = 0 if args.quiet else args.verbose
    _setup_logging(verbosity)

    # --- Resolve paths ---
    try:
        script_dir = _resolve_script_dir(args.script_dir)
    except ValueError as e:
        logger.error("%s", e)
        return 1

    flutter_dir_name = args.flutter_dir_name or script_dir.name

    depot_tools_dir = (
        Path(args.depot_tools_dir).resolve()
        if args.depot_tools_dir
        else Path.home() / ".local" / "depot_tools"
    )

    # --- Parse and validate configs (before checking paths, for clearer errors) ---
    targets: list[BuildTarget] = []
    for cfg_str in args.configs:
        try:
            targets.append(BuildTarget.parse(cfg_str))
        except ValueError as e:
            logger.error("%s", e)
            return 1

    host_targets = [t for t in targets if t.is_host]
    if not host_targets:
        logger.error(
            "At least one host build target is required (e.g. host_debug). "
            "Provided configs: %s",
            ", ".join(args.configs),
        )
        return 1

    logger.info("Resolved %d build target(s): %s", len(targets), ", ".join(t.name for t in targets))

    # --- Resolve engine source path ---
    engine_src = Path(args.engine_src).resolve() if args.engine_src else script_dir / "engine" / "src"
    if not engine_src.is_dir():
        logger.error("Engine source directory not found: %s", engine_src)
        return 1

    # --- Compression ---
    comp_program = args.compression_program
    if comp_program is None:
        comp_program = _detect_compression_program()
    elif not shutil.which(comp_program):
        logger.error("Requested compressor '%s' not found in PATH", comp_program)
        return 1

    threads = args.compression_threads
    if threads == 0:
        threads = os.cpu_count() or 4

    # --- Ninja selection ---
    # autoninja from depot_tools auto-tunes -j but recent versions try to
    # start Chromium's Android build server (fast_local_dev_server.py).
    # That file doesn't exist in Flutter engine checkouts, causing a
    # FileNotFoundError.  We set ANDROID_BUILD_SERVER_DISABLED=1 as a
    # workaround, but if that doesn't help (depot_tools version too old
    # to respect it), fall back to plain ninja.
    ninja_program = args.ninja
    if ninja_program is None:
        if shutil.which("autoninja"):
            ninja_program = "autoninja"
            logger.info(
                "Selected autoninja (set ANDROID_BUILD_SERVER_DISABLED=1 "
                "to work around Chromium build server issue). "
                "Use --ninja=ninja if builds still fail."
            )
        elif shutil.which("ninja"):
            ninja_program = "ninja"
        else:
            logger.error(
                "Neither autoninja nor ninja found in PATH. "
                "Ensure depot_tools is installed or pass --depot-tools-dir."
            )
            return 1
    elif not shutil.which(ninja_program):
        logger.error("Requested ninja binary '%s' not found in PATH", ninja_program)
        return 1

    # --- Build config and run ---
    cfg = PackagerConfig(
        version=args.version,
        script_dir=script_dir,
        engine_src=engine_src,
        flutter_dir_name=flutter_dir_name,
        configs=targets,
        strip_debug_symbols=args.strip_debug_symbols,
        depot_tools_dir=depot_tools_dir,
        compression_program=comp_program,
        compression_level=args.compression_level,
        compression_threads=threads,
        ninja_program=ninja_program,
        skip_build=args.skip_build,
        dry_run=args.dry_run,
    )

    try:
        packager = Packager(cfg)
        packager.run()
    except FileNotFoundError as e:
        logger.error("File not found: %s", e)
        return 1
    except RuntimeError as e:
        logger.error("Packaging failed: %s", e)
        return 1
    except subprocess.CalledProcessError as e:
        logger.error("Build command failed (exit %d): %s", e.returncode, e.cmd)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())