package com.androidperformance.standalone;

import android.os.Bundle;

import androidx.benchmark.macro.CompilationMode;
import androidx.benchmark.macro.StartupMode;
import androidx.benchmark.macro.StartupTimingMetric;
import androidx.benchmark.macro.junit4.MacrobenchmarkRule;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.Collections;

import kotlin.Unit;

@RunWith(AndroidJUnit4.class)
public final class StandaloneStartupBenchmark {
    private static final int DEFAULT_ITERATIONS = 5;

    @Rule
    public final MacrobenchmarkRule benchmarkRule = new MacrobenchmarkRule();

    @Test
    public void startup() {
        Bundle arguments = InstrumentationRegistry.getArguments();
        String targetPackage = requireTargetPackage(arguments);
        int iterations = parseIterations(arguments);

        benchmarkRule.measureRepeated(
                targetPackage,
                Collections.singletonList(new StartupTimingMetric()),
                CompilationMode.DEFAULT,
                StartupMode.COLD,
                iterations,
                scope -> {
                    scope.pressHome();
                    return Unit.INSTANCE;
                },
                scope -> {
                    scope.startActivityAndWait(intent -> Unit.INSTANCE);
                    return Unit.INSTANCE;
                }
        );
    }

    private static String requireTargetPackage(Bundle arguments) {
        String value = arguments.getString("targetPackage");
        if (value == null || !value.matches("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+")) {
            throw new IllegalArgumentException(
                    "Missing or invalid instrumentation argument: targetPackage"
            );
        }
        return value;
    }

    private static int parseIterations(Bundle arguments) {
        String value = arguments.getString("iterations");
        if (value == null || value.isBlank()) {
            return DEFAULT_ITERATIONS;
        }
        try {
            int parsed = Integer.parseInt(value);
            if (parsed < 1 || parsed > 100) {
                throw new IllegalArgumentException("iterations must be between 1 and 100");
            }
            return parsed;
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("iterations must be an integer", error);
        }
    }
}
