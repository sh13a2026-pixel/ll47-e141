// LL47 e141 — Custom Flutter entry point
// Gọi initFirebaseAndFCM() trước khi Flet Python app khởi động.

import 'package:flutter/material.dart';
import 'package:flet/flet.dart';
import 'firebase_init.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Khởi tạo Firebase + lấy FCM token trước khi Flet chạy
  await initFirebaseAndFCM();

  // Khởi động Flet app bình thường
  await createFletApp();
}
