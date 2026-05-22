$ErrorActionPreference = "Stop"

$SdkManager = "C:\Users\nhat anh\AppData\Local\Android\sdk\cmdline-tools\latest\bin\sdkmanager.bat"
$AvdManager = "C:\Users\nhat anh\AppData\Local\Android\sdk\cmdline-tools\latest\bin\avdmanager.bat"
$Emulator = "C:\Users\nhat anh\AppData\Local\Android\sdk\emulator\emulator.exe"
$ImageName = "system-images;android-33;google_apis;x86_64"
$AvdName = "LL47_Emulator"

Write-Host "1. Đang tải HĐH Android 13 (API 33) cho máy ảo (khoảng 1.4GB, vui lòng đợi và ấn 'y' nếu được hỏi)..." -ForegroundColor Cyan
& $SdkManager $ImageName

Write-Host "2. Đang tạo thiết bị ảo tên là $AvdName..." -ForegroundColor Cyan
echo "no" | & $AvdManager create avd -n $AvdName -k $ImageName --device "pixel" --force

Write-Host "3. Khởi động máy ảo (Cửa sổ điện thoại sẽ tự động mở lên)..." -ForegroundColor Cyan
Write-Host "-> Sau khi màn hình điện thoại ảo sáng lên, hãy mở một cửa sổ Powershell KHÁC và gõ 'flutter run' để chạy app!" -ForegroundColor Yellow
& $Emulator -avd $AvdName
