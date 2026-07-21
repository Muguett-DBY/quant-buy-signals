plugins {
    id("com.android.application")
}

val androidReleaseSigningEnvironment = mapOf(
    "DS_DCF_ANDROID_KEYSTORE" to System.getenv("DS_DCF_ANDROID_KEYSTORE"),
    "DS_DCF_ANDROID_STORE_PASSWORD" to System.getenv("DS_DCF_ANDROID_STORE_PASSWORD"),
    "DS_DCF_ANDROID_KEY_ALIAS" to System.getenv("DS_DCF_ANDROID_KEY_ALIAS"),
    "DS_DCF_ANDROID_KEY_PASSWORD" to System.getenv("DS_DCF_ANDROID_KEY_PASSWORD"),
)
val verifyReleaseSigningInputs = tasks.register("verifyReleaseSigningInputs") {
    doLast {
        val missing = androidReleaseSigningEnvironment
            .filterValues { it.isNullOrBlank() }
            .keys
            .sorted()
        if (missing.isNotEmpty()) {
            throw GradleException("Signed release build is missing: ${missing.joinToString(", ")}")
        }
        val keyStore = file(androidReleaseSigningEnvironment.getValue("DS_DCF_ANDROID_KEYSTORE")!!)
        if (!keyStore.isFile) {
            throw GradleException("Signed release keystore does not exist: $keyStore")
        }
    }
}

android {
    namespace = "com.muguett.dsdcf"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.muguett.dsdcf"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "11.2.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        create("release") {
            val keyStorePath = androidReleaseSigningEnvironment["DS_DCF_ANDROID_KEYSTORE"]
            storeFile = if (keyStorePath.isNullOrBlank()) null else file(keyStorePath)
            storePassword = androidReleaseSigningEnvironment["DS_DCF_ANDROID_STORE_PASSWORD"]
            keyAlias = androidReleaseSigningEnvironment["DS_DCF_ANDROID_KEY_ALIAS"]
            keyPassword = androidReleaseSigningEnvironment["DS_DCF_ANDROID_KEY_PASSWORD"]
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }
}

tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    dependsOn(verifyReleaseSigningInputs)
}

dependencies {
    implementation("androidx.core:core:1.15.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
