import re
import os

print("--- Start Patching Build Configs ---")

# Detect workspace path
temp_workspace = r"temp_build_src/build/flutter/android"
default_workspace = r"build/flutter/android"

if os.path.exists(temp_workspace):
    workspace = temp_workspace
    print("Found temp_build_src workspace for patching")
else:
    workspace = default_workspace
    print("Using default build workspace for patching")

settings_path = os.path.join(workspace, "settings.gradle")
wrapper_path = os.path.join(workspace, "gradle", "wrapper", "gradle-wrapper.properties")
appbuild_path = os.path.join(workspace, "app", "build.gradle")
gradleproperties_path = os.path.join(workspace, "gradle.properties")
topbuild_path = os.path.join(workspace, "build.gradle")

# 1. Patch settings.gradle (AGP + Kotlin Plugin declaration)
if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        s = f.read()
    # Replace AGP and also inject Kotlin plugin version
    s = s.replace('version "8.3.1" apply false', 'version "8.9.1" apply false\n    id "org.jetbrains.kotlin.android" version "2.0.21" apply false')
    with open(settings_path, "w", encoding="utf-8") as f:
        f.write(s)
    print("  settings.gradle: AGP 8.9.1 and Kotlin 2.0.21 patched")
else:
    print(f"  [X] Settings file not found: {settings_path}")

# 2. Patch gradle-wrapper.properties
if os.path.exists(wrapper_path):
    with open(wrapper_path, "r", encoding="utf-8") as f:
        s = f.read()
    s = re.sub(r'gradle-[\d.]+-bin\.zip', 'gradle-8.11.1-bin.zip', s)
    with open(wrapper_path, "w", encoding="utf-8") as f:
        f.write(s)
    print("  gradle-wrapper.properties: Gradle 8.11.1 patched")
else:
    print(f"  [X] Wrapper file not found: {wrapper_path}")

# 3. Patch app/build.gradle
if os.path.exists(appbuild_path):
    with open(appbuild_path, "r", encoding="utf-8") as f:
        s = f.read()
    s = s.replace('VERSION_1_8', 'VERSION_11')
    s = s.replace("jvmTarget = '1.8'", "jvmTarget = '11'")
    s = s.replace('minSdkVersion flutter.minSdkVersion', 'minSdkVersion 21')
    
    # Thay thế ndkVersion cũ bằng bản mới
    s = re.sub(r'ndkVersion\s+["\'][\d.]+["\']', 'ndkVersion "28.2.13676358"', s)
    
    with open(appbuild_path, "w", encoding="utf-8") as f:
        f.write(s)
    print("  app/build.gradle: Java 11, minSdkVersion 21, ndkVersion 28 patched")
else:
    print(f"  [X] App build file not found: {appbuild_path}")

# 4. Patch gradle.properties
if os.path.exists(gradleproperties_path):
    with open(gradleproperties_path, "r", encoding="utf-8") as f:
        s = f.read()
    if 'android.ndk.suppressMinSdkVersionError' not in s:
        s += "\nandroid.ndk.suppressMinSdkVersionError=21\n"
    if 'kotlin.jvm.target.validation.mode' not in s:
        s += "\nkotlin.jvm.target.validation.mode=IGNORE\n"
    with open(gradleproperties_path, "w", encoding="utf-8") as f:
        f.write(s)
    print("  gradle.properties: NDK error bypass and Kotlin JVM validation IGNORE patched")
else:
    print(f"  [X] gradle.properties not found: {gradleproperties_path}")

# 5. Patch top-level build.gradle (Kotlin version to 2.0.21)
if os.path.exists(topbuild_path):
    with open(topbuild_path, "r", encoding="utf-8") as f:
        s = f.read()
    s = s.replace("ext.kotlin_version = '1.9.24'", "ext.kotlin_version = '2.0.21'")
    with open(topbuild_path, "w", encoding="utf-8") as f:
        f.write(s)
    print("  build.gradle: Kotlin version 2.0.21 patched")
else:
    print(f"  [X] Top build file not found: {topbuild_path}")

print("--- End Patching ---")
