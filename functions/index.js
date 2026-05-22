const { onDocumentCreated } = require("firebase-functions/v2/firestore");
const admin = require("firebase-admin");

admin.initializeApp();

exports.processFCMQueue = onDocumentCreated("fcm_queue/{docId}", async (event) => {
  const snapshot = event.data;
  if (!snapshot) {
    return;
  }

  const payload = snapshot.data();
  const { to, title, body, data, link } = payload;

  if (!to) {
    console.log("No 'to' field provided, skipping.");
    return;
  }

  // Khởi tạo thông điệp (message)
  const message = {
    notification: {
      title: title || "Thông báo",
      body: body || "",
    },
    android: {
      notification: {
        sound: "default",
        channelId: "high_importance_channel"
      }
    },
    apns: {
      payload: {
        aps: {
          sound: "default"
        }
      }
    },
    data: data || {},
  };
  
  if (link) {
      message.data.link = link;
  }

  try {
    let response;
    // Kiểm tra xem có phải gửi broadcast không
    if (to === "all" || to === "room-all" || to === "all_users") {
      message.topic = "all_users";
      response = await admin.messaging().send(message);
      console.log(`Sent to topic all_users:`, response);
    } else {
      // Tìm token thiết bị của user (to = uid)
      const tokensSnap = await admin.firestore().collection("users").doc(to).collection("fcm_tokens").get();
      if (tokensSnap.empty) {
          console.log(`No device tokens found for user: ${to}`);
      } else {
          const tokens = [];
          tokensSnap.forEach(doc => {
              const t = doc.data().token;
              if (t) tokens.push(t);
          });
          
          if (tokens.length > 0) {
              message.tokens = tokens;
              response = await admin.messaging().sendEachForMulticast(message);
              console.log(`Sent to user ${to} (${tokens.length} devices). Success count: ${response.successCount}`);
          }
      }
    }

    // Xoá document khỏi queue sau khi xử lý xong (dù thành công hay không có token)
    await snapshot.ref.delete();
  } catch (error) {
    console.error("Error sending FCM:", error);
  }
});
