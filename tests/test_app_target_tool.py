from __future__ import annotations

from pathlib import Path

from tools.app_target_tool import InspectAppTargetTool


ANDROID_NS = "http://schemas.android.com/apk/res/android"


def write_settings(root: Path, modules: list[str]) -> None:
    includes = ", ".join(f'":{module}"' for module in modules)
    (root / "settings.gradle.kts").write_text(
        f"include({includes})\n",
        encoding="utf-8",
    )


def write_module(root: Path, module: str, build_text: str) -> Path:
    module_path = root / module
    module_path.mkdir(parents=True)
    filename = "build.gradle.kts" if "applicationId =" in build_text else "build.gradle"
    (module_path / filename).write_text(build_text, encoding="utf-8")
    return module_path


def write_manifest(module_path: Path, application_body: str) -> None:
    manifest = module_path / "src" / "main" / "AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f'''<manifest xmlns:android="{ANDROID_NS}">
    <application>
        {application_body}
    </application>
</manifest>
''',
        encoding="utf-8",
    )


def launcher_activity(name: str) -> str:
    return f'''<activity android:name="{name}">
    <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
    </intent-filter>
</activity>'''


def execute(root: Path, module: str | None = None) -> dict:
    arguments = {"project_path": str(root)}
    if module is not None:
        arguments["module"] = module
    return InspectAppTargetTool(allowed_project_path=root).execute(arguments)


def test_single_application_module_with_groovy_application_id(
    tmp_path: Path,
) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id "com.android.application" }
android {
    namespace "com.example.code"
    defaultConfig { applicationId "com.example.app" }
}
''',
    )
    write_manifest(module, launcher_activity(".MainActivity"))

    result = execute(tmp_path)

    assert result["success"] is True
    assert result["module"] == "app"
    assert result["application_id"] == "com.example.app"
    assert result["namespace"] == "com.example.code"
    assert result["launcher_activity"] == "com.example.code.MainActivity"
    assert result["launcher_component"] == (
        "com.example.app/com.example.code.MainActivity"
    )
    assert result["variant"] == "debug"


def test_kotlin_dsl_application_id_and_unqualified_activity(
    tmp_path: Path,
) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id("com.android.application") }
android {
    namespace = "com.example.kotlin"
    defaultConfig { applicationId = "com.example.kotlin.app" }
}
''',
    )
    write_manifest(module, launcher_activity("MainActivity"))

    result = execute(tmp_path)

    assert result["application_id"] == "com.example.kotlin.app"
    assert result["launcher_activity"] == "com.example.kotlin.MainActivity"


def test_multiple_application_modules_require_explicit_choice(
    tmp_path: Path,
) -> None:
    write_settings(tmp_path, ["app", "demo"])
    write_module(tmp_path, "app", 'plugins { id "com.android.application" }')
    write_module(tmp_path, "demo", 'plugins { id "com.android.application" }')

    result = execute(tmp_path)

    assert result["success"] is False
    assert result["error_type"] == "MULTIPLE_APPLICATION_MODULES"
    assert result["candidates"] == ["app", "demo"]
    assert result["module"] is None


def test_explicit_module_is_used_when_multiple_exist(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app", "demo"])
    write_module(tmp_path, "app", 'plugins { id "com.android.application" }')
    module = write_module(
        tmp_path,
        "demo",
        '''plugins { id "com.android.application" }
android {
    namespace "com.example.demo"
    defaultConfig { applicationId "com.example.demo" }
}
''',
    )
    write_manifest(module, launcher_activity(".DemoActivity"))

    result = execute(tmp_path, module=":demo")

    assert result["success"] is True
    assert result["module"] == "demo"


def test_no_application_module(tmp_path: Path) -> None:
    write_settings(tmp_path, ["library"])
    write_module(tmp_path, "library", 'plugins { id "com.android.library" }')

    result = execute(tmp_path)

    assert result["error_type"] == "NO_APPLICATION_MODULE"
    assert result["candidates"] == []


def test_unknown_requested_module_returns_candidates(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app"])
    write_module(tmp_path, "app", 'plugins { id "com.android.application" }')

    result = execute(tmp_path, module="missing")

    assert result["error_type"] == "MODULE_NOT_FOUND"
    assert result["candidates"] == ["app"]


def test_fully_qualified_launcher_activity(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id "com.android.application" }
android {
    namespace "com.example.code"
    defaultConfig { applicationId "com.example.app" }
}
''',
    )
    write_manifest(module, launcher_activity("com.other.EntryActivity"))

    result = execute(tmp_path)

    assert result["launcher_activity"] == "com.other.EntryActivity"
    assert result["launcher_component"] == (
        "com.example.app/com.other.EntryActivity"
    )


def test_activity_alias_launcher_uses_alias_component(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id "com.android.application" }
android {
    namespace "com.example.code"
    defaultConfig { applicationId "com.example.app" }
}
''',
    )
    write_manifest(
        module,
        '''<activity android:name=".MainActivity" />
<activity-alias
    android:name=".LauncherAlias"
    android:targetActivity=".MainActivity">
    <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
    </intent-filter>
</activity-alias>''',
    )

    result = execute(tmp_path)

    assert result["launcher_activity"] == "com.example.code.MainActivity"
    assert result["launcher_component"] == (
        "com.example.app/com.example.code.LauncherAlias"
    )


def test_missing_launcher_is_reported_without_guessing(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id "com.android.application" }
android {
    namespace "com.example.app"
    defaultConfig { applicationId "com.example.app" }
}
''',
    )
    write_manifest(module, '<activity android:name=".MainActivity" />')

    result = execute(tmp_path)

    assert result["success"] is False
    assert result["error_type"] == "LAUNCHER_ACTIVITY_NOT_FOUND"
    assert result["launcher_activity"] is None
    assert result["launcher_component"] is None


def test_unresolved_application_id_is_not_replaced_by_namespace(
    tmp_path: Path,
) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id("com.android.application") }
android {
    namespace = "com.example.code"
    defaultConfig { applicationId = providers.gradleProperty("appId").get() }
    productFlavors {
        create("demo") { applicationId = "com.example.demo" }
    }
}
''',
    )
    write_manifest(module, launcher_activity(".MainActivity"))

    result = execute(tmp_path)

    assert result["success"] is False
    assert result["error_type"] == "APPLICATION_ID_UNRESOLVED"
    assert result["application_id"] is None
    assert result["namespace"] == "com.example.code"
    assert result["launcher_activity"] == "com.example.code.MainActivity"
    assert result["launcher_component"] is None
    assert result["best_effort"] is True


def test_flavor_specific_application_id_is_unresolved(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app"])
    module = write_module(
        tmp_path,
        "app",
        '''plugins { id("com.android.application") }
android {
    namespace = "com.example.code"
    defaultConfig { applicationId = "com.example.base" }
    productFlavors {
        create("demo") { applicationIdSuffix = ".demo" }
    }
}
''',
    )
    write_manifest(module, launcher_activity(".MainActivity"))

    result = execute(tmp_path)

    assert result["error_type"] == "APPLICATION_ID_UNRESOLVED"
    assert result["application_id"] is None


def test_missing_manifest_is_reported(tmp_path: Path) -> None:
    write_settings(tmp_path, ["app"])
    write_module(
        tmp_path,
        "app",
        '''plugins { id "com.android.application" }
android { defaultConfig { applicationId "com.example.app" } }
''',
    )

    result = execute(tmp_path)

    assert result["error_type"] == "MANIFEST_NOT_FOUND"
    assert result["manifest_path"] is None
