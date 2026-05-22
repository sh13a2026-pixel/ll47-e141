import 'package:isar/isar.dart';

part 'conversation.g.dart';

@collection
class Conversation {
  Id id = Isar.autoIncrement;
  
  @Index(unique: true, replace: true)
  String? serverId; // ID phòng chat trên MongoDB

  String? name;
  String? lastMessage;
  DateTime? updatedAt;
}
