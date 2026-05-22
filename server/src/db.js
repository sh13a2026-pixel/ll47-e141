"use strict";
/**
 * Kết nối MongoDB + khởi tạo GridFSBucket + tạo index.
 */
const { MongoClient, GridFSBucket } = require("mongodb");
const dns = require("dns");

// Force dùng Google DNS để resolve SRV record (tránh DNS nội bộ chặn)
dns.setServers(["8.8.8.8", "8.8.4.4", "1.1.1.1"]);

let client = null;
let db = null;
let bucket = null;

async function connect() {
  const uri = process.env.MONGODB_URI || "mongodb://localhost:27017";
  const dbName = process.env.MONGODB_DB || "ll47";

  client = new MongoClient(uri, {
    maxPoolSize: 20,
    serverSelectionTimeoutMS: 30000,
    connectTimeoutMS: 30000,
    family: 4,
  });
  await client.connect();
  db = client.db(dbName);
  bucket = new GridFSBucket(db, { bucketName: "uploads" });

  // Index — tạo idempotent, an toàn gọi mỗi lần khởi động.
  await db.collection("documents").createIndex({ _path: 1 }, { unique: true });
  await db.collection("documents").createIndex({ _parent: 1 });
  await db.collection("auth_users").createIndex({ email: 1 }, { unique: true });
  await db.collection("auth_users").createIndex({ uid: 1 }, { unique: true });
  await db.collection("refresh_tokens").createIndex({ token: 1 }, { unique: true });
  await db.collection("refresh_tokens").createIndex({ uid: 1 });
  // TTL cho refresh token đã hết hạn (Mongo tự dọn).
  await db
    .collection("refresh_tokens")
    .createIndex({ expiresAt: 1 }, { expireAfterSeconds: 0 });

  // Index cho Chat app
  await db.collection("conversations").createIndex({ participants: 1 });
  await db.collection("messages").createIndex({ conversationId: 1, createdAt: 1 });


  console.log(`[db] Đã kết nối MongoDB: db="${dbName}"`);
  return db;
}

function getDb() {
  if (!db) throw new Error("MongoDB chưa kết nối. Gọi connect() trước.");
  return db;
}

function getBucket() {
  if (!bucket) throw new Error("GridFS chưa sẵn sàng. Gọi connect() trước.");
  return bucket;
}

async function close() {
  if (client) await client.close();
  client = db = bucket = null;
}

module.exports = { connect, getDb, getBucket, close };
