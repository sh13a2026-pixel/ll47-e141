"use strict";
/**
 * LL47 e141 backend — điểm khởi động.
 * Express + Socket.io + MongoDB (thay Firebase: Auth, Firestore, Storage, FCM queue).
 */
require("dotenv").config();

const http = require("http");
const express = require("express");
const cors = require("cors");
const { Server } = require("socket.io");

const db = require("./db");
const auth = require("./auth");
const documents = require("./documents");
const storage = require("./storage");
const realtime = require("./realtime");
const fcm = require("./fcm");

const PORT = parseInt(process.env.PORT || "8080", 10);
const CORS_ORIGIN = (process.env.CORS_ORIGIN || "*")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const corsOptions = { origin: CORS_ORIGIN.length === 1 && CORS_ORIGIN[0] === "*" ? "*" : CORS_ORIGIN };

const app = express();
app.use(cors(corsOptions));

const jsonParser = express.json({ limit: "20mb" });

// 1) Tải file công khai (không cần auth) — phải đứng trước mount /storage có auth.
app.use(storage.publicRouter);

// 2) Health check / root
app.get("/health", (req, res) => res.json({ ok: true, ts: Date.now() }));
app.get("/", (req, res) => res.json({ name: "ll47-backend", status: "ok" }));

// 3) Auth (không yêu cầu token sẵn)
app.use("/auth", jsonParser, auth.router);

// 4) Storage upload/delete (cần token; upload tự parse raw body bên trong)
app.use("/storage", auth.authRequired, storage.router);

// 5) Doc-store + query (cần token + json)
app.use(auth.authRequired, jsonParser, documents);

// 404 cuối cùng (chỉ tới được nếu qua được authRequired ở trên)
app.use((req, res) => res.status(404).json({ error: { message: "NOT_FOUND" } }));

async function main() {
  await db.connect();

  const server = http.createServer(app);
  const io = new Server(server, { cors: corsOptions });
  realtime.init(io);

  fcm.startWorker();

  server.listen(PORT, () => {
    console.log(`[server] LL47 backend nghe tại cổng ${PORT}`);
    console.log(`[server] PUBLIC_URL = ${process.env.PUBLIC_URL || "(chưa đặt)"}`);
  });
}

main().catch((e) => {
  console.error("[server] khởi động thất bại:", e);
  process.exit(1);
});
