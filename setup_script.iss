; ============================================================
;  Inno Setup Script — Quản lý LL47 e141 v2.6.0
;  Tạo bộ cài Windows đầy đủ: chọn thư mục, shortcut, uninstall
; ============================================================

#define AppName      "Quản lý LL47 e141"
#define AppVersion   "2.6.0"
#define AppPublisher "Trung đoàn 141"
#define AppExeName   "QuanLyLL47.exe"
#define AppFolder    "dist\QuanLyLL47"

[Setup]
; --- Thông tin app ---
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright=Copyright © 2025 {#AppPublisher}

; --- Thư mục cài mặc định (người dùng có thể đổi) ---
DefaultDirName={autopf}\LL47_e141
DefaultGroupName={#AppName}

; --- Output ---
OutputDir=dist
OutputBaseFilename=LL47_E141_Setup_v{#AppVersion}
SetupIconFile=assets\logo.ico

; --- Nén ---
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; --- Giao diện ---
; Cho phép người dùng chọn thư mục cài
DisableDirPage=no
; Ẩn trang chọn Start Menu group (đơn giản hơn)
DisableProgramGroupPage=yes
; Hiện wizard đẹp (ảnh bên trái 164x314)
WizardStyle=modern

; --- Quyền: thử admin trước, nếu không có thì cài cho user hiện tại ---
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest

; --- Uninstall ---
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

; --- Misc ---
; Không cho cài 2 bản cùng lúc
AppMutex=LL47e141AppMutex
; Tự đóng app cũ nếu đang chạy khi update
CloseApplications=yes
CloseApplicationsFilter=*{#AppExeName}*
RestartApplications=no

[Languages]
; Dùng tiếng Anh (Inno Setup không có sẵn tiếng Việt built-in)
Name: "english"; MessagesFile: "compiler:Default.isl"

; ============================================================
;  Tuỳ chọn khi cài
; ============================================================
[Tasks]
; Shortcut Desktop — mặc định được chọn
Name: "desktopicon"; \
  Description: "Tạo biểu tượng ngoài màn hình Desktop"; \
  GroupDescription: "Tuỳ chọn:"; \
  Flags: checkedonce

; Shortcut Quick Launch (thanh taskbar cũ — Windows 7 trở xuống)
Name: "quicklaunchicon"; \
  Description: "Tạo biểu tượng trên thanh Quick Launch"; \
  GroupDescription: "Tuỳ chọn:"; \
  Flags: unchecked; OnlyBelowVersion: 6.1

; Khởi động cùng Windows
Name: "startup"; \
  Description: "Tự khởi động khi đăng nhập Windows"; \
  GroupDescription: "Tuỳ chọn:"; \
  Flags: unchecked

; ============================================================
;  Files — đóng gói toàn bộ folder PyInstaller output
; ============================================================
[Files]
; Toàn bộ nội dung dist\QuanLyLL47\ → {app}\
Source: "{#AppFolder}\*"; \
  DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; ============================================================
;  Shortcuts
; ============================================================
[Icons]
; Start Menu
Name: "{group}\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; \
  Comment: "Quản lý Lực lượng 47 — Trung đoàn 141"

; Gỡ cài đặt trong Start Menu
Name: "{group}\Gỡ cài đặt {#AppName}"; \
  Filename: "{uninstallexe}"

; Desktop shortcut (nếu người dùng chọn)
Name: "{autodesktop}\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; \
  Tasks: desktopicon; \
  Comment: "Quản lý Lực lượng 47 — Trung đoàn 141"

; Quick Launch (Windows 7-)
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  Tasks: quicklaunchicon

; Startup (nếu người dùng chọn)
Name: "{userstartup}\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; \
  Tasks: startup

; ============================================================
;  Registry — để Add/Remove Programs hiện đúng thông tin
; ============================================================
[Registry]
Root: HKLM; \
  Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\LL47e141"; \
  ValueType: string; ValueName: "DisplayName"; \
  ValueData: "{#AppName} v{#AppVersion}"; \
  Flags: uninsdeletekey; \
  Check: IsAdminInstallMode

Root: HKCU; \
  Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\LL47e141"; \
  ValueType: string; ValueName: "DisplayName"; \
  ValueData: "{#AppName} v{#AppVersion}"; \
  Flags: uninsdeletekey; \
  Check: not IsAdminInstallMode

; ============================================================
;  Chạy app sau khi cài xong (tuỳ chọn)
; ============================================================
[Run]
Filename: "{app}\{#AppExeName}"; \
  Description: "Chạy {#AppName} ngay bây giờ"; \
  Flags: nowait postinstall skipifsilent; \
  WorkingDir: "{app}"

; ============================================================
;  Dọn dẹp khi gỡ cài đặt
; ============================================================
[UninstallDelete]
; Xoá file dữ liệu người dùng nếu muốn (comment lại nếu muốn giữ)
; Type: filesandordirs; Name: "{localappdata}\LL47e141"
Type: dirifempty; Name: "{app}"
