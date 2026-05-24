"use strict";
/**
 * Realtime qua Socket.io — thay cho cơ chế "listen bằng polling" của Firestore.
 *
 * Client gọi:  socket.emit("subscribe",   { collection: "chat_rooms/r1/messages" })
 *              socket.emit("unsubscribe", { collection: "..." })
 * Khi có thay đổi trong collection đó, server phát:
 *              socket.on("change", ({ collection }) => { ...fetch lại... })
 *
 * Presence (online status):
 *   Client gọi:  socket.emit("user_online",  { uid })  — sau khi login
 *   Client gọi:  socket.emit("user_offline", { uid })  — khi disconnect / logout
 *   Server phát: socket.on("presence_update", { uid, online: true/false })
 *   Server phát: socket.on("presence_list",   { uids: [...] })  — khi mới kết nối
 *
 * Giữ payload nhẹ (chỉ tên collection) — client tự gọi REST để lấy dữ liệu mới,
 * khớp đúng mô hình callback(list) cũ.
 */
let _io = null;

// Map uid -> Set<socketId> — một user có thể mở nhiều tab/thiết bị
const _onlineUids = new Map();

function _addOnline(uid, socketId) {
  if (!_onlineUids.has(uid)) _onlineUids.set(uid, new Set());
  _onlineUids.get(uid).add(socketId);
}

function _removeOnline(uid, socketId) {
  const s = _onlineUids.get(uid);
  if (!s) return;
  s.delete(socketId);
  if (s.size === 0) _onlineUids.delete(uid);
}

function getOnlineUids() {
  return Array.from(_onlineUids.keys());
}

function init(io) {
  _io = io;
  io.on("connection", (socket) => {
    let _socketUid = null; // uid đã đăng ký cho socket này

    socket.on("subscribe", (msg) => {
      const col = msg && msg.collection;
      if (typeof col === "string" && col) socket.join(`col:${col}`);
    });
    socket.on("unsubscribe", (msg) => {
      const col = msg && msg.collection;
      if (typeof col === "string" && col) socket.leave(`col:${col}`);
    });

    // ── PRESENCE ──────────────────────────────────────────────────────────
    // Client gửi ngay sau khi kết nối + login thành công
    socket.on("user_online", (msg) => {
      const uid = msg && msg.uid;
      if (!uid) return;
      _socketUid = uid;
      _addOnline(uid, socket.id);
      // Gửi danh sách online hiện tại cho client mới vào
      socket.emit("presence_list", { uids: getOnlineUids() });
      // Broadcast cho tất cả: user này vừa online
      io.emit("presence_update", { uid, online: true });
    });

    // Client gửi khi logout (tuỳ chọn — disconnect cũng tự xử lý)
    socket.on("user_offline", (msg) => {
      const uid = (msg && msg.uid) || _socketUid;
      if (!uid) return;
      _removeOnline(uid, socket.id);
      if (!_onlineUids.has(uid)) {
        io.emit("presence_update", { uid, online: false });
      }
      _socketUid = null;
    });

    // Tự động xử lý khi mất kết nối
    socket.on("disconnect", () => {
      if (!_socketUid) return;
      _removeOnline(_socketUid, socket.id);
      if (!_onlineUids.has(_socketUid)) {
        io.emit("presence_update", { uid: _socketUid, online: false });
      }
    });
    // ── END PRESENCE ───────────────────────────────────────────────────────

    // === CHAT REALTIME FEATURES ===
    
    // Tham gia phòng chat (khi mở màn hình chat)
    socket.on("join_room", (msg) => {
      const { conversationId } = msg;
      if (conversationId) socket.join(`room:${conversationId}`);
    });

    // Rời phòng chat
    socket.on("leave_room", (msg) => {
      const { conversationId } = msg;
      if (conversationId) socket.leave(`room:${conversationId}`);
    });

    // Gửi tin nhắn mới
    socket.on("send_message", async (msg) => {
      try {
        const db = require("./db").getDb();
        const newMsg = {
          conversationId: msg.conversationId,
          senderId: msg.senderId,
          content: msg.content,
          type: msg.type || "text",
          createdAt: new Date(),
          status: "sent"
        };
        
        // Lưu tin nhắn vào MongoDB (để đồng bộ)
        const result = await db.collection("messages").insertOne(newMsg);
        newMsg._id = result.insertedId;
        newMsg.tempId = msg.tempId; // ID tạm từ client để client biết tin nào đã gửi thành công

        // Cập nhật message mới nhất vào conversation
        await db.collection("conversations").updateOne(
          { _id: new require("mongodb").ObjectId(msg.conversationId) },
          { $set: { lastMessage: newMsg.content, updatedAt: new Date() } }
        );

        // Bắn tin nhắn qua cho tất cả user trong phòng (kể cả người gửi để xác nhận)
        io.to(`room:${msg.conversationId}`).emit("new_message", newMsg);
        
        // FIXME: Tại đây có thể kích hoạt worker gửi FCM cho các user đang offline
      } catch (err) {
        console.error("Lỗi gửi tin nhắn:", err);
      }
    });

    // Sự kiện Đang gõ...
    socket.on("typing", (msg) => {
      socket.to(`room:${msg.conversationId}`).emit("typing", { 
        userId: msg.userId, 
        conversationId: msg.conversationId 
      });
    });

    socket.on("stop_typing", (msg) => {
      socket.to(`room:${msg.conversationId}`).emit("stop_typing", { 
        userId: msg.userId, 
        conversationId: msg.conversationId 
      });
    });

    // Đánh dấu đã đọc
    socket.on("mark_as_read", async (msg) => {
      const { conversationId, messageId, readerId } = msg;
      // Cập nhật MongoDB (tùy chọn theo logic team, ví dụ lưu mảng readBy)
      socket.to(`room:${conversationId}`).emit("message_read", { messageId, readerId, conversationId });
    });
  });
}

/** Phát tín hiệu "collection này vừa thay đổi" cho mọi client đang subscribe. */
function emitChange(collectionPath) {
  if (!_io || !collectionPath) return;
  _io.to(`col:${collectionPath}`).emit("change", { collection: collectionPath });
}

module.exports = { init, emitChange, getOnlineUids };
