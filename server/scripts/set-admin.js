"use strict";
/**
 * Script một lần: cấp quyền super admin (adminLevel=5, isAdmin=true)
 * cho một user theo email hoặc username (số quân).
 *
 * Cách dùng:
 *   node scripts/set-admin.js 001
 *   node scripts/set-admin.js 001@ll47.local
 *   node scripts/set-admin.js <email-bất-kỳ>
 *
 * Chạy từ thư mục server/:
 *   cd server && node scripts/set-admin.js 001
 */

require("dotenv").config({ path: require("path").join(__dirname, "..", ".env") });

const { MongoClient } = require("mongodb");
const dns = require("dns");
dns.setServers(["8.8.8.8", "8.8.4.4"]);

const MONGODB_URI = process.env.MONGODB_URI || "mongodb://localhost:27017";
const MONGODB_DB  = process.env.MONGODB_DB  || "ll47";

async function main() {
  const arg = (process.argv[2] || "").trim();
  if (!arg) {
    console.error("Dùng: node scripts/set-admin.js <username hoặc email>");
    process.exit(1);
  }

  // Chuẩn hoá thành email ll47.local nếu chỉ nhập số quân
  const email = arg.includes("@") ? arg.toLowerCase() : `${arg.toLowerCase()}@ll47.local`;

  const client = new MongoClient(MONGODB_URI, {
    serverSelectionTimeoutMS: 15000,
    connectTimeoutMS: 15000,
    family: 4,
    tls: true,
    tlsAllowInvalidCertificates: false,
  });

  try {
    await client.connect();
    const db = client.db(MONGODB_DB);

    // 1. Tìm uid trong auth_users
    const authUser = await db.collection("auth_users").findOne({ email });
    if (!authUser) {
      console.error(`Không tìm thấy tài khoản với email: ${email}`);
      console.error("Kiểm tra lại số quân / email, hoặc tài khoản chưa đăng ký.");
      process.exit(1);
    }

    const { uid } = authUser;
    console.log(`Tìm thấy: email=${email}  uid=${uid}`);

    // 2. Upsert profile trong documents collection
    // Dữ liệu app được lưu trong subdoc "data" (GET /doc/<path> trả về row.data).
    const path = `users/${uid}`;
    const now = new Date();
    await db.collection("documents").updateOne(
      { _path: path },
      {
        $set: {
          "data.isAdmin":    true,
          "data.adminLevel": 5,
          updatedAt:         now,
        },
        $setOnInsert: { _path: path, _parent: "users", _id: uid, createdAt: now },
      },
      { upsert: true }
    );

    const updated = await db.collection("documents").findOne({ _path: path });
    const d = updated?.data || {};
    console.log(`Đã cập nhật ${path}:`);
    console.log(`  data.isAdmin    = ${d.isAdmin}`);
    console.log(`  data.adminLevel = ${d.adminLevel}`);
    console.log("Xong. Khởi động lại app để áp dụng.");
  } finally {
    await client.close();
  }
}

main().catch((err) => {
  console.error("Lỗi:", err.message || err);
  process.exit(1);
});
