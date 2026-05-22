import 'package:isar/isar.dart';

part 'message.g.dart'; // Isar generator sẽ tạo file này

@collection
class Message {
  Id id = Isar.autoIncrement;

  @Index(unique: true, replace: true)
  String? tempId; // Dùng để match offline message khi server trả về ID thật

  String? serverId;
  String? conversationId;
  String? senderId;
  String? content;
  String? type; // text, image, etc.
  
  @Index()
  DateTime? createdAt;

  // Trạng thái: 'sending', 'sent', 'read', 'error'
  String? status; 
}
