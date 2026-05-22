import 'package:socket_io_client/socket_io_client.dart' as IO;
import '../collections/message.dart';
import 'database_service.dart';

class SocketService {
  static late IO.Socket socket;

  static void init(String serverUrl) {
    socket = IO.io(serverUrl, <String, dynamic>{
      'transports': ['websocket'],
      'autoConnect': false,
    });

    socket.connect();

    socket.onConnect((_) {
      print('Connected to Socket.IO server');
    });

    // Lắng nghe tin nhắn mới từ server
    socket.on('new_message', (data) async {
      // Dữ liệu từ server: { _id, conversationId, senderId, content, type, tempId, status, createdAt }
      final tempId = data['tempId'];
      final serverId = data['_id'];

      if (tempId != null) {
        // Đây là tin nhắn do chính mình vừa gửi, server trả về để xác nhận
        await DatabaseService.updateMessageStatus(tempId, serverId, 'sent');
      } else {
        // Tin nhắn từ người khác gửi đến
        final newMsg = Message()
          ..serverId = serverId
          ..conversationId = data['conversationId']
          ..senderId = data['senderId']
          ..content = data['content']
          ..type = data['type']
          ..status = 'sent'
          ..createdAt = DateTime.parse(data['createdAt']);
          
        await DatabaseService.saveLocalMessage(newMsg);
      }
    });

    socket.onDisconnect((_) => print('Disconnected from Socket.IO server'));
  }

  static void joinRoom(String conversationId) {
    socket.emit('join_room', {'conversationId': conversationId});
  }

  // Gửi tin nhắn
  static void sendMessage(Message msg) {
    socket.emit('send_message', {
      'tempId': msg.tempId,
      'conversationId': msg.conversationId,
      'senderId': msg.senderId,
      'content': msg.content,
      'type': msg.type,
    });
  }
}
