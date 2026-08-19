# Standalone Macrobenchmark Harness PoC

This is an isolated `com.android.test` project used to validate whether AndroidX
Macrobenchmark can measure an already-installed external application by package name.
It does not reference or modify the target application's Gradle project.

AGP requires every `com.android.test` module to declare a non-null
`targetProjectPath`. The local `:harness-host` module exists only to satisfy that
build-model constraint. Because the Benchmark module is self-instrumenting, its
instrumentation targets the Benchmark APK itself; `:harness-host` is not installed or
used as the measured application. The external measured app is still selected only by
the runtime `targetPackage` argument.

The target package is supplied at runtime as the `targetPackage` instrumentation
argument. Reliability errors such as `DEBUGGABLE`, `NOT-PROFILEABLE`, `EMULATOR`,
and `LOW-BATTERY` are not suppressed.

Run from the Android Performance Agent repository root:

```bash
./harness/run_standalone_benchmark.sh \
  --serial f91e097e \
  --target-package com.sample.redex
```

Use `--iterations N` to override the default five iterations. The target APK must
already be installed. The script builds and installs only the Harness APK, executes
the startup benchmark, and pulls Benchmark JSON and Perfetto traces into
`harness/results/`.

Build only the Harness:

```bash
cd harness/standalone-macrobenchmark
./gradlew --no-daemon :benchmark:assembleDebug
```

Install only the self-instrumenting Benchmark APK:

```bash
adb -s SERIAL install -r \
  benchmark/build/outputs/apk/debug/benchmark-debug.apk
```

The runner script additionally verifies that the installed target APK is
non-debuggable, profileable by shell, and contains the ProfileInstaller receiver.
It requires `ANDROID_HOME` so it can inspect the installed target APK with `aapt2`.

See [POC_RESULTS.md](POC_RESULTS.md) for the physical-device Chapter22 A/B result.
