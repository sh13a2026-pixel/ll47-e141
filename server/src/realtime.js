"use strict";
/**
 * Realtime qua Socket.io — thay cho cơ chế "listen bằng polling" của Firestore.
 *
 * Client gọi:  socket.emit("subscribe",   { collection: "chat_rooms/r1/messages" })
 *              socket.emit("unsubscribe", { collection: "..." })
 * Khi có thay đổi trong collection đó, server phát:
 *              socket.on("change", ({ collection }) => { ...fetch lại... })
 *
 * Giữ payload nhẹ (chỉ tên collection) — client tự gọi REST để lấy dữ liệu mới,
 * khớp đúng mô hình callback(list) cũ.
 */
let _io = null;

function init(io) {
  _io = io;
  io.on("connection", (socket) => {
    socket.on("subscribe", (msg) => {
      const col = msg && msg.collection;
      if (typeof col === "string" && col) socket.join(`col:${col}`);
    });
    socket.on("unsubscribe", (msg) => {
      const col = msg && msg.collection;
      if (typeof col === "string" && col) socket.leave(`col:${col}`);
    });

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

module.exports = { init, emitChange };
