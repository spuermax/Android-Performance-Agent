#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$SCRIPT_DIR/standalone-macrobenchmark"
RESULTS_ROOT="$SCRIPT_DIR/results"
HARNESS_PACKAGE="com.androidperformance.standalone"
TEST_CLASS="$HARNESS_PACKAGE.StandaloneStartupBenchmark"
RUNNER="androidx.test.runner.AndroidJUnitRunner"

SERIAL=""
TARGET_PACKAGE=""
ITERATIONS="5"

usage() {
    echo "Usage: $0 --serial SERIAL --target-package PACKAGE [--iterations N]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --serial)
            SERIAL="${2:-}"
            shift 2
            ;;
        --target-package)
            TARGET_PACKAGE="${2:-}"
            shift 2
            ;;
        --iterations)
            ITERATIONS="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$SERIAL" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    echo "Invalid or missing --serial." >&2
    exit 2
fi
if [[ ! "$TARGET_PACKAGE" =~ ^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$ ]]; then
    echo "Invalid or missing --target-package." >&2
    exit 2
fi
if [[ ! "$ITERATIONS" =~ ^[0-9]+$ ]] || (( ITERATIONS < 1 || ITERATIONS > 100 )); then
    echo "--iterations must be between 1 and 100." >&2
    exit 2
fi
if ! command -v adb >/dev/null 2>&1; then
    echo "ADB_NOT_FOUND: adb is not available on PATH." >&2
    exit 3
fi
if [[ ! -x "$HARNESS_DIR/gradlew" ]]; then
    echo "GRADLE_WRAPPER_NOT_FOUND: $HARNESS_DIR/gradlew" >&2
    exit 3
fi

DEVICE_STATE="$(adb -s "$SERIAL" get-state 2>/dev/null || true)"
if [[ "$DEVICE_STATE" != "device" ]]; then
    echo "DEVICE_NOT_READY: $SERIAL is not an online ADB device." >&2
    exit 4
fi
if ! adb -s "$SERIAL" shell pm path "$TARGET_PACKAGE" >/dev/null 2>&1; then
    echo "TARGET_NOT_INSTALLED: $TARGET_PACKAGE is not installed on $SERIAL." >&2
    exit 5
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_RESULTS="/sdcard/Download/standalone-macrobenchmark-$TIMESTAMP"
LOCAL_RESULTS="$RESULTS_ROOT/$TIMESTAMP"
mkdir -p "$LOCAL_RESULTS"

PACKAGE_DUMP_FILE="$LOCAL_RESULTS/target-package-dump.txt"
TARGET_MANIFEST_FILE="$LOCAL_RESULTS/target-manifest.txt"
TARGET_APK_FILE="$LOCAL_RESULTS/target.apk"
adb -s "$SERIAL" shell dumpsys package "$TARGET_PACKAGE" > "$PACKAGE_DUMP_FILE"
TARGET_APK_DEVICE="$(adb -s "$SERIAL" shell pm path "$TARGET_PACKAGE" | sed -n 's/^package://p' | head -n 1 | tr -d '\r')"
adb -s "$SERIAL" pull "$TARGET_APK_DEVICE" "$TARGET_APK_FILE" >/dev/null

if [[ -z "${ANDROID_HOME:-}" ]]; then
    echo "ANDROID_HOME_NOT_SET: required to inspect the installed target APK." >&2
    exit 6
fi
AAPT2="$(ls "$ANDROID_HOME"/build-tools/*/aapt2 2>/dev/null | sort -V | tail -n 1)"
if [[ ! -x "$AAPT2" ]]; then
    echo "AAPT2_NOT_FOUND: required to inspect the installed target APK." >&2
    exit 6
fi
"$AAPT2" dump xmltree "$TARGET_APK_FILE" --file AndroidManifest.xml > "$TARGET_MANIFEST_FILE"

if grep -q 'android:debuggable.*=true' "$TARGET_MANIFEST_FILE"; then
    echo "TARGET_DEBUGGABLE: refusing to benchmark a debuggable target." >&2
    exit 7
fi
if ! grep -q 'E: profileable' "$TARGET_MANIFEST_FILE" || \
   ! grep -q 'android:shell.*=true' "$TARGET_MANIFEST_FILE"; then
    echo "TARGET_NOT_PROFILEABLE: target is not profileable by shell." >&2
    exit 8
fi
if ! grep -q 'androidx.profileinstaller.ProfileInstallReceiver' "$TARGET_MANIFEST_FILE"; then
    echo "PROFILER_INSTALLER_NOT_FOUND: COLD StartupMode shader-cache reset may fail on an unrooted device." >&2
    exit 9
fi

echo "Building standalone Harness only..."
"$HARNESS_DIR/gradlew" -p "$HARNESS_DIR" --no-daemon :benchmark:assembleDebug

HARNESS_APK="$HARNESS_DIR/benchmark/build/outputs/apk/debug/benchmark-debug.apk"
if [[ ! -f "$HARNESS_APK" ]]; then
    echo "HARNESS_APK_NOT_FOUND: $HARNESS_APK" >&2
    exit 8
fi

echo "Installing Harness APK..."
adb -s "$SERIAL" install -r "$HARNESS_APK" >/dev/null

adb -s "$SERIAL" shell mkdir -p "$REMOTE_RESULTS"

echo "Running COLD Startup Macrobenchmark against $TARGET_PACKAGE..."
set +e
adb -s "$SERIAL" shell am instrument -w -r \
    -e class "$TEST_CLASS" \
    -e targetPackage "$TARGET_PACKAGE" \
    -e iterations "$ITERATIONS" \
    -e additionalTestOutputDir "$REMOTE_RESULTS" \
    "$HARNESS_PACKAGE/$RUNNER" | tee "$LOCAL_RESULTS/instrumentation.log"
INSTRUMENTATION_STATUS=${PIPESTATUS[0]}
set -e

adb -s "$SERIAL" pull "$REMOTE_RESULTS" "$LOCAL_RESULTS/device-output" >/dev/null

JSON_FILES="$(find "$LOCAL_RESULTS" -type f -name '*.json' -print)"
TRACE_FILES="$(find "$LOCAL_RESULTS" -type f -name '*.perfetto-trace' -print)"
JSON_COUNT="$(printf '%s\n' "$JSON_FILES" | sed '/^$/d' | wc -l | tr -d ' ')"
TRACE_COUNT="$(printf '%s\n' "$TRACE_FILES" | sed '/^$/d' | wc -l | tr -d ' ')"

echo "Instrumentation exit status: $INSTRUMENTATION_STATUS"
echo "Local result directory: $LOCAL_RESULTS"
echo "Benchmark JSON files: $JSON_COUNT"
printf '%s\n' "$JSON_FILES" | sed 's/^/  /'
echo "Perfetto trace files: $TRACE_COUNT"
printf '%s\n' "$TRACE_FILES" | sed 's/^/  /'

if (( INSTRUMENTATION_STATUS != 0 )); then
    exit "$INSTRUMENTATION_STATUS"
fi
if (( JSON_COUNT == 0 || TRACE_COUNT == 0 )); then
    echo "BENCHMARK_OUTPUT_MISSING: expected Benchmark JSON and Perfetto trace files." >&2
    exit 9
fi
