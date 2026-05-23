// LL47 e141 — Firebase / FCM initializer
// Chạy trước khi Flet Python app khởi động.
// Lấy FCM token → lưu vào SharedPreferences với key "fcm_token"
// để Python đọc qua page.client_storage.get_async("fcm_token").

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:shared_preferences/shared_preferences.dart';

// Handler xử lý message khi app đang BACKGROUND / đã đóng
@pragma('vm:entry-point')
Future<void> _firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  // Firebase phải được khởi tạo lại ở isolate background
  await Firebase.initializeApp();
  // Background message được hệ điều hành hiện tự động (notification payload).
  // Không cần làm gì thêm ở đây — chỉ cần hàm này tồn tại.
}

Future<void> initFirebaseAndFCM() async {
  try {
    await Firebase.initializeApp();

    // Đăng ký background handler (BẮT BUỘC phải gọi trước khi app vào foreground)
    FirebaseMessaging.onBackgroundMessage(_firebaseMessagingBackgroundHandler);

    final messaging = FirebaseMessaging.instance;

    // Xin quyền thông báo (iOS + Android 13+)
    await messaging.requestPermission(
      alert: true,
      badge: true,
      sound: true,
    );

    // Lấy FCM token
    final token = await messaging.getToken();
    if (token != null && token.isNotEmpty) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('fcm_token', token);
    }

    // Cập nhật lại token mỗi khi refresh (thiết bị mới, reinstall...)
    messaging.onTokenRefresh.listen((newToken) async {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('fcm_token', newToken);
    });
  } catch (_) {
    // Firebase chưa cấu hình → bỏ qua, app vẫn chạy bình thường
  }
}
