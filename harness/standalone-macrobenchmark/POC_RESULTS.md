# Standalone Macrobenchmark Harness PoC Result

Date: 2026-08-19

Verdict: **VIABLE**, with an AGP build-model limitation documented below.

## Environment

- Device: Xiaomi M2102K1AC (`f91e097e`), physical device
- Android: 14 / API 34
- Battery: 100%, USB powered, 34.0°C battery temperature before/after the runs
- Target APK: Chapter22 `ReDexSample-benchmark.apk`
- Target package: `com.sample.redex`
- Target state: non-debuggable, `profileable android:shell="true"`, release-derived
  benchmark build type
- Target ProfileInstaller: 1.4.1 receiver present
- Startup mode: COLD
- Compilation mode: DEFAULT
- Metric: `StartupTimingMetric`
- Iterations: 5 per round, two rounds per implementation
- Reliability suppressions: none

The runs were alternated A1, B1, A2, B2. A is Chapter22's existing in-project
Macrobenchmark. B is this Standalone Harness. Both measured the same installed target
APK on the same device.

## Benchmark JSON results

All values are milliseconds and come from Benchmark JSON, not Gradle or ADB console
timings.

| Run | Harness | Minimum | Median | Maximum | Runs |
| --- | --- | ---: | ---: | ---: | --- |
| A1 | Chapter22 in-project | 284.860157 | 287.850417 | 391.348959 | 391.348959, 287.850417, 284.860157, 286.689687, 326.343125 |
| B1 | Standalone | 297.881562 | 307.235417 | 327.920469 | 327.920469, 297.881562, 303.760052, 323.259062, 307.235417 |
| A2 | Chapter22 in-project | 289.727812 | 320.949791 | 335.501094 | 335.501094, 295.504583, 327.690469, 289.727812, 320.949791 |
| B2 | Standalone | 299.940261 | 310.231615 | 326.717760 | 317.918125, 326.717760, 299.940261, 310.231615, 300.290417 |

Pooled across ten iterations per implementation:

| Harness | Minimum | Median | Maximum |
| --- | ---: | ---: | ---: |
| Chapter22 in-project | 284.860157 | 308.227187 | 391.348959 |
| Standalone | 297.881562 | 308.733516 | 327.920469 |

Standalone pooled median difference relative to the in-project benchmark:

```text
(308.733516 - 308.227187) / 308.227187 * 100 = +0.164271%
```

Neither result contained `timeToFullDisplayMs`, so TTFD was not reported by this app.

## Trace validation

Each of the four rounds produced one Benchmark JSON file and five
`.perfetto-trace` files. Perfetto Trace Processor v57.2 was used manually for this PoC
to query `android_startups` in all 20 traces. Every trace contained exactly the expected
startup for:

```text
package = com.sample.redex
startup_type = cold
```

The traces therefore cover the same target package and cold-start interval. Both sets
show normal Android App Startup data. No extra target-app startup attributable to the
Standalone Harness was found.

Generated artifacts are intentionally ignored by Git under `harness/results/`.

## Architecture and limitations

The Benchmark APK is self-instrumenting: its generated manifest sets both the package
and instrumentation target to `com.androidperformance.standalone`. It never runs in
the `com.sample.redex` process.

AGP 8.13.2 rejects a `com.android.test` project whose `targetProjectPath` is null. The
Harness therefore contains an inert local `:harness-host` application module solely to
satisfy that build-model requirement. The host has no target-app code, is not installed
by the runner, and is not the measured package. The user project remains completely
outside the Harness Gradle graph; the only runtime relationship to the measured app is
the `targetPackage` instrumentation argument.

The Chapter22 in-project benchmark uses AndroidX Benchmark 1.2.0-beta01, while the
Standalone Harness uses stable 1.4.1. This is a residual A/B difference because the
Chapter22 project was deliberately not modified. The close pooled medians are
encouraging, but broader device/app sampling is still required before treating the
result as universal equivalence.

For COLD startup on an unrooted device, Macrobenchmark uses ProfileInstaller hooks to
drop shader cache. `CompilationMode.DEFAULT` also uses an available Baseline Profile.
The tested target includes ProfileInstaller 1.4.1. The runner reports
`PROFILER_INSTALLER_NOT_FOUND` instead of modifying a target that lacks this receiver.

No Chapter22 source or Gradle file was modified during this PoC.
