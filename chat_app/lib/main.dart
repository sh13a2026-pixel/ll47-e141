import 'package:flutter/material.dart';
import 'collections/message.dart';
import 'services/database_service.dart';
import 'services/socket_service.dart';
import 'package:uuid/uuid.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  
  String? initError;
  try {
    await DatabaseService.init();
  } catch (e) {
    initError = 'Lỗi khởi tạo Database: $e';
    debugPrint(initError);
  }
  
  // URL của Node.js Backend (Tạm tắt vì chưa có server)
  // SocketService.init('http://10.0.2.2:8080'); // Dùng cho máy ảo Android (localhost)
  // SocketService.init('http://192.168.x.x:8080'); // Dùng cho điện thoại thật (IP máy tính)
  
  runApp(ChatApp(initError: initError));
}

class ChatApp extends StatelessWidget {
  final String? initError;
  const ChatApp({super.key, this.initError});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'LL47 Chat',
      theme: ThemeData(primarySwatch: Colors.blue),
      home: initError != null
          ? Scaffold(
              appBar: AppBar(title: const Text('LL47 Chat')),
              body: Center(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(
                    initError!,
                    style: const TextStyle(color: Colors.red, fontSize: 16),
                    textAlign: TextAlign.center,
                  ),
                ),
              ),
            )
          : const ChatScreen(conversationId: 'room1', currentUserId: 'user1'),
    );
  }
}

class ChatScreen extends StatefulWidget {
  final String conversationId;
  final String currentUserId;

  const ChatScreen({
    super.key,
    required this.conversationId,
    required this.currentUserId,
  });

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final TextEditingController _textController = TextEditingController();
  final _uuid = const Uuid();

  @override
  void initState() {
    super.initState();
    SocketService.joinRoom(widget.conversationId);
  }

  void _sendMessage() async {
    if (_textController.text.trim().isEmpty) return;

    final content = _textController.text;
    _textController.clear();

    final tempId = _uuid.v4();
    final newMsg = Message()
      ..tempId = tempId
      ..conversationId = widget.conversationId
      ..senderId = widget.currentUserId
      ..content = content
      ..type = 'text'
      ..createdAt = DateTime.now()
      ..status = 'sending';

    // 1. Lưu Offline vào Isar ngay lập tức (Offline-First)
    await DatabaseService.saveLocalMessage(newMsg);

    // 2. Gửi qua Socket.io
    SocketService.sendMessage(newMsg);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Chat Room')),
      body: Column(
        children: [
          Expanded(
            child: StreamBuilder<List<Message>>(
              stream: DatabaseService.watchMessages(widget.conversationId),
              builder: (context, snapshot) {
                if (!snapshot.hasData) return const Center(child: CircularProgressIndicator());
                
                final messages = snapshot.data!;
                return ListView.builder(
                  itemCount: messages.length,
                  itemBuilder: (context, index) {
                    final msg = messages[index];
                    final isMe = msg.senderId == widget.currentUserId;
                    
                    return Align(
                      alignment: isMe ? Alignment.centerRight : Alignment.centerLeft,
                      child: Container(
                        margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: isMe ? Colors.blue[100] : Colors.grey[200],
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            Text(msg.content ?? ''),
                            if (isMe) ...[
                              const SizedBox(height: 4),
                              Icon(
                                msg.status == 'sent' ? Icons.check_circle : Icons.access_time,
                                size: 12,
                                color: msg.status == 'sent' ? Colors.blue : Colors.grey,
                              )
                            ]
                          ],
                        ),
                      ),
                    );
                  },
                );
              },
            ),
          ),
          Padding(
            padding: const EdgeInsets.all(8.0),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _textController,
                    decoration: const InputDecoration(hintText: 'Nhập tin nhắn...'),
                  ),
                ),
                IconButton(
                  icon: const Icon(Icons.send),
                  onPressed: _sendMessage,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
