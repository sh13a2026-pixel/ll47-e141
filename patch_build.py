import re
import os

print("--- Start Patching Build Configs ---")

settings_path = r"build/flutter/android/settings.gradle"
wrapper_path = r"build/flutter/android/gradle/wrapper/gradle-wrapper.properties"
appbuild_path = r"build/flutter/android/app/build.gradle"
gradleproperties_path = r"build/flutter/android/gradle.properties"

# 1. Patch settings.gradle
if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        s = f.read()
    s = s.replace('version "8.3.1" apply false', 'version "8.9.1" apply false')
    with open(settings_path, "w", encoding="utf-8") as f:
        f.write(s)
    print("  settings.gradle: AGP 8.9.1 patched")
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
        with open(gradleproperties_path, "w", encoding="utf-8") as f:
            f.write(s)
        print("  gradle.properties: suppressMinSdkVersionError=21 patched")
else:
    print(f"  [X] gradle.properties not found: {gradleproperties_path}")

print("--- End Patching ---")
