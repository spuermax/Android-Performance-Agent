plugins {
    id("com.android.application")
}

android {
    namespace = "com.androidperformance.standalone.host"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.androidperformance.standalone.host"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }
}
