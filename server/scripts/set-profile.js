"use strict";
/**
 * Script cập nhật thông tin cá nhân cho một tài khoản (name, rank, role, unitId, unitName).
 *
 * Cách dùng (chạy từ thư mục server/):
 *   node scripts/set-profile.js 001 "Nguyễn Văn An" "Thiếu tá" "Chỉ huy trưởng" "e141" "Trung đoàn 141"
 *
 * Tham số (theo thứ tự):
 *   1. username hoặc email   (bắt buộc)
 *   2. name                  (bắt buộc)
 *   3. rank                  (tuỳ chọn — bỏ trống: "")
 *   4. role                  (tuỳ chọn — bỏ trống: "")
 *   5. unitId                (tuỳ chọn — bỏ trống: "")
 *   6. unitName              (tuỳ chọn — bỏ trống: "")
 */

require("dotenv").config({ path: require("path").join(__dirname, "..", ".env") });

const { MongoClient } = require("mongodb");
const dns = require("dns");
dns.setServers(["8.8.8.8", "8.8.4.4"]);

const MONGODB_URI = process.env.MONGODB_URI || "mongodb://localhost:27017";
const MONGODB_DB  = process.env.MONGODB_DB  || "ll47";

async function main() {
  const [,, argUser, argName, argRank, argRole, argUnitId, argUnitName] = process.argv;

  if (!argUser || !argName) {
    console.error("Dùng: node scripts/set-profile.js <username/email> <name> [rank] [role] [unitId] [unitName]");
    console.error('Ví dụ: node scripts/set-profile.js 001 "Nguyễn Văn An" "Thiếu tá" "Chỉ huy trưởng"');
    process.exit(1);
  }

  const email = argUser.includes("@") ? argUser.toLowerCase() : `${argUser.toLowerCase()}@ll47.local`;

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

    // 1. Tìm uid
    const authUser = await db.collection("auth_users").findOne({ email });
    if (!authUser) {
      console.error(`Không tìm thấy tài khoản với email: ${email}`);
      process.exit(1);
    }

    const { uid } = authUser;
    console.log(`Tìm thấy: email=${email}  uid=${uid}`);

    // 2. Build $set object — chỉ ghi các field được truyền vào
    const $set = {
      "data.name":     argName,
      "data.username": argUser.includes("@") ? argUser.split("@")[0] : argUser,
      updatedAt: new Date(),
    };
    if (argRank    !== undefined) $set["data.rank"]     = argRank;
    if (argRole    !== undefined) $set["data.role"]     = argRole;
    if (argUnitId  !== undefined) $set["data.unitId"]   = argUnitId;
    if (argUnitName !== undefined) $set["data.unitName"] = argUnitName;

    const path = `users/${uid}`;
    await db.collection("documents").updateOne(
      { _path: path },
      {
        $set,
        $setOnInsert: { _path: path, _parent: "users", _id: uid, createdAt: new Date() },
      },
      { upsert: true }
    );

    const updated = await db.collection("documents").findOne({ _path: path });
    const d = updated?.data || {};
    console.log(`\nĐã cập nhật ${path}:`);
    console.log(`  data.name     = ${d.name}`);
    console.log(`  data.rank     = ${d.rank}`);
    console.log(`  data.role     = ${d.role}`);
    console.log(`  data.unitId   = ${d.unitId}`);
    console.log(`  data.unitName = ${d.unitName}`);
    console.log(`  data.isAdmin  = ${d.isAdmin}`);
    console.log(`  data.adminLevel = ${d.adminLevel}`);
    console.log("\nXong. Đăng nhập lại để áp dụng.");
  } finally {
    await client.close();
  }
}

main().catch((err) => {
  console.error("Lỗi:", err.message || err);
  process.exit(1);
});
