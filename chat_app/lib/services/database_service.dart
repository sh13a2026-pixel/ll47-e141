import 'package:isar/isar.dart';
import 'package:path_provider/path_provider.dart';
import '../collections/message.dart';
import '../collections/conversation.dart';

class DatabaseService {
  static late Isar isar;

  static Future<void> init() async {
    final dir = await getApplicationDocumentsDirectory();
    isar = await Isar.open(
      [MessageSchema, ConversationSchema],
      directory: dir.path,
    );
  }

  // Lưu tin nhắn offline (trạng thái: sending)
  static Future<void> saveLocalMessage(Message msg) async {
    await isar.writeTxn(() async {
      await isar.messages.put(msg);
    });
  }

  // Cập nhật tin nhắn khi server xác nhận đã gửi thành công
  static Future<void> updateMessageStatus(String tempId, String serverId, String status) async {
    final msg = await isar.messages.where().tempIdEqualTo(tempId).findFirst();
    if (msg != null) {
      msg.serverId = serverId;
      msg.status = status;
      await isar.writeTxn(() async {
        await isar.messages.put(msg);
      });
    }
  }

  // Lấy danh sách tin nhắn của 1 phòng chat theo thứ tự thời gian
  static Stream<List<Message>> watchMessages(String conversationId) {
    return isar.messages
        .filter()
        .conversationIdEqualTo(conversationId)
        .sortByCreatedAt()
        .build()
        .watch(fireImmediately: true);
  }
}
