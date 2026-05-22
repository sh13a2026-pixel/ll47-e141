$FlutterDir = "D:\flutter"
if (-Not (Test-Path $FlutterDir)) {
    Write-Host "Đang tải mã nguồn Flutter SDK (nhánh stable)..."
    git clone https://github.com/flutter/flutter.git -b stable $FlutterDir
} else {
    Write-Host "Thư mục $FlutterDir đã tồn tại."
}

# Add to current session PATH
$env:Path += ";$FlutterDir\bin"

# Add to User PATH permanently
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notmatch [regex]::Escape("$FlutterDir\bin")) {
    $NewPath = $UserPath + ";$FlutterDir\bin"
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "Đã thêm Flutter vào biến môi trường PATH."
}

# Run flutter doctor
Write-Host "Đang chạy 'flutter doctor' để tải các công cụ cần thiết (Dart SDK...)"
& "$FlutterDir\bin\flutter.bat" doctor
