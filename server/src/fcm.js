"use strict";
/**
 * FCM worker — gửi push nền (khi app đóng) qua firebase-admin.
 *
 * Client ghi 1 document vào collection `fcm_queue` (qua doc-store) với payload:
 *   { to, title, body, link, data, createdAt, sent:false }
 * Worker này quét hàng đợi định kỳ, gửi qua FCM/APNs rồi xoá document.
 * Đây là bản thay thế Cloud Function `processFCMQueue` cũ.
 *
 * Lưu ý: vẫn cần FCM/APNs để đẩy thông báo khi app KHÔNG mở (Socket.io chỉ
 * realtime khi app đang chạy). Nếu chưa cấu hình service account thì worker
 * bỏ qua, mọi thứ khác vẫn hoạt động bình thường.
 */
const path = require("path");
const { getDb } = require("./db");

let messaging = null;
let initialized = false;
let warnedMissing = false;

function initAdmin() {
  if (initialized) return messaging;
  initialized = true;
  const admin = require("firebase-admin");
  let cred = null;
  try {
    const raw = process.env.FIREBASE_SERVICE_ACCOUNT;
    const credPath = process.env.FIREBASE_SERVICE_ACCOUNT_PATH;
    if (raw && raw.trim()) {
      cred = JSON.parse(raw);
    } else if (credPath && credPath.trim()) {
      cred = require(path.resolve(credPath));
    }
  } catch (e) {
    console.error("[fcm] Không đọc được service account:", e.message);
  }
  if (!cred) return null;
  if (!admin.apps.length) {
    admin.initializeApp({ credential: admin.credential.cert(cred) });
  }
  messaging = admin.messaging();
  console.log("[fcm] firebase-admin đã sẵn sàng — push nền BẬT.");
  return messaging;
}

async function tokensForUser(uid) {
  const rows = await getDb()
    .collection("documents")
    .find({ _parent: `users/${uid}/fcm_tokens` })
    .toArray();
  return rows.map((r) => (r.data && r.data.token) || "").filter(Boolean);
}

function buildMessage(payload) {
  const message = {
    notification: {
      title: payload.title || "Thông báo",
      body: payload.body || "",
    },
    android: { notification: { sound: "default", channelId: "high_importance_channel" } },
    apns: { payload: { aps: { sound: "default" } } },
    data: {},
  };
  // FCM data payload chỉ nhận string.
  const src = payload.data || {};
  for (const [k, v] of Object.entries(src)) message.data[k] = String(v);
  if (payload.link) message.data.link = String(payload.link);
  return message;
}

async function processOne(msg, doc) {
  const db = getDb();
  const payload = doc.data || {};
  const to = payload.to;
  try {
    if (to && ["all", "room-all", "all_users"].includes(to)) {
      const message = buildMessage(payload);
      message.topic = "all_users";
      await msg.send(message);
    } else if (to) {
      const tokens = await tokensForUser(to);
      if (tokens.length) {
        const message = buildMessage(payload);
        message.tokens = tokens;
        const resp = await msg.sendEachForMulticast(message);
        console.log(`[fcm] gửi tới ${to}: ${resp.successCount}/${tokens.length} OK`);
      }
    }
  } catch (e) {
    console.error("[fcm] lỗi gửi:", e.message);
  } finally {
    // Xoá khỏi hàng đợi dù thành công hay không (giống Cloud Function cũ).
    await db.collection("documents").deleteOne({ _path: doc._path });
  }
}

async function tick() {
  const msg = initAdmin();
  if (!msg) {
    if (!warnedMissing) {
      warnedMissing = true;
      console.warn(
        "[fcm] Chưa cấu hình FIREBASE_SERVICE_ACCOUNT — push nền TẮT (app vẫn chạy)."
      );
    }
    return;
  }
  const queue = await getDb()
    .collection("documents")
    .find({ _parent: "fcm_queue" })
    .limit(25)
    .toArray();
  for (const doc of queue) {
    await processOne(msg, doc);
  }
}

function startWorker(intervalMs = 4000) {
  setInterval(() => {
    tick().catch((e) => console.error("[fcm] worker tick error", e.message));
  }, intervalMs);
  console.log(`[fcm] worker khởi động (quét mỗi ${intervalMs}ms).`);
}

module.exports = { startWorker };
