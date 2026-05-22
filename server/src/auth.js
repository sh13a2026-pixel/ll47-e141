"use strict";
/**
 * Auth backend — thay thế Firebase Identity Toolkit.
 *
 * Trả về shape tương thích với client cũ:
 *   { idToken, refreshToken, localId, email, expiresIn }
 *
 * Lỗi trả về dạng { error: { message: "MA_LOI" } } để client map sang
 * thông báo tiếng Việt (EMAIL_EXISTS, INVALID_LOGIN_CREDENTIALS, ...).
 */
const crypto = require("crypto");
const express = require("express");
const bcrypt = require("bcryptjs");
const jwt = require("jsonwebtoken");
const { getDb } = require("./db");

const router = express.Router();

const JWT_SECRET = () => process.env.JWT_SECRET || "dev-insecure-secret";
const JWT_TTL = () => parseInt(process.env.JWT_EXPIRES_IN || "3600", 10);
const REFRESH_TTL_DAYS = () => parseInt(process.env.REFRESH_TTL_DAYS || "30", 10);

function errJson(res, status, code) {
  return res.status(status).json({ error: { message: code } });
}

function makeUid() {
  return crypto.randomBytes(14).toString("hex"); // 28 ký tự, giống localId Firebase
}

function normalizeEmail(email) {
  return String(email || "").trim().toLowerCase();
}

async function issueCreds(user) {
  const db = getDb();
  const idToken = jwt.sign(
    { uid: user.uid, email: user.email },
    JWT_SECRET(),
    { expiresIn: JWT_TTL() }
  );
  const refreshToken = crypto.randomBytes(32).toString("hex");
  const expiresAt = new Date(Date.now() + REFRESH_TTL_DAYS() * 86400 * 1000);
  await db
    .collection("refresh_tokens")
    .insertOne({ token: refreshToken, uid: user.uid, email: user.email, expiresAt });
  return {
    idToken,
    refreshToken,
    localId: user.uid,
    email: user.email,
    expiresIn: String(JWT_TTL()),
  };
}

// ---- Middleware: yêu cầu Bearer token hợp lệ ----
function authRequired(req, res, next) {
  const h = req.headers.authorization || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  if (!m) return errJson(res, 401, "MISSING_ID_TOKEN");
  try {
    const payload = jwt.verify(m[1], JWT_SECRET());
    req.auth = { uid: payload.uid, email: payload.email };
    return next();
  } catch (e) {
    const code = e && e.name === "TokenExpiredError" ? "TOKEN_EXPIRED" : "INVALID_ID_TOKEN";
    return errJson(res, 401, code);
  }
}

function verifyIdToken(token) {
  return jwt.verify(token, JWT_SECRET());
}

// ===========================================================================
// Routes
// ===========================================================================

// Đăng ký
router.post("/signup", async (req, res) => {
  try {
    const email = normalizeEmail(req.body.email);
    const password = String(req.body.password || "");
    if (!email || !email.includes("@")) return errJson(res, 400, "INVALID_EMAIL");
    if (password.length < 6) return errJson(res, 400, "WEAK_PASSWORD");

    const db = getDb();
    const existing = await db.collection("auth_users").findOne({ email });
    if (existing) return errJson(res, 400, "EMAIL_EXISTS");

    const uid = makeUid();
    const passwordHash = await bcrypt.hash(password, 10);
    await db
      .collection("auth_users")
      .insertOne({ uid, email, passwordHash, createdAt: new Date() });

    return res.json(await issueCreds({ uid, email }));
  } catch (e) {
    console.error("[auth] signup error", e);
    return errJson(res, 500, "INTERNAL");
  }
});

// Đăng nhập
router.post("/signin", async (req, res) => {
  try {
    const email = normalizeEmail(req.body.email);
    const password = String(req.body.password || "");
    const db = getDb();
    const user = await db.collection("auth_users").findOne({ email });
    if (!user) return errJson(res, 400, "INVALID_LOGIN_CREDENTIALS");
    if (user.disabled) return errJson(res, 400, "USER_DISABLED");
    const ok = await bcrypt.compare(password, user.passwordHash || "");
    if (!ok) return errJson(res, 400, "INVALID_LOGIN_CREDENTIALS");
    return res.json(await issueCreds({ uid: user.uid, email: user.email }));
  } catch (e) {
    console.error("[auth] signin error", e);
    return errJson(res, 500, "INTERNAL");
  }
});

// Làm mới idToken bằng refresh token
router.post("/refresh", async (req, res) => {
  try {
    const refreshToken = String(req.body.refreshToken || req.body.refresh_token || "");
    if (!refreshToken) return errJson(res, 400, "INVALID_REFRESH_TOKEN");
    const db = getDb();
    const rec = await db.collection("refresh_tokens").findOne({ token: refreshToken });
    if (!rec) return errJson(res, 400, "INVALID_REFRESH_TOKEN");
    if (rec.expiresAt && rec.expiresAt.getTime() < Date.now()) {
      await db.collection("refresh_tokens").deleteOne({ token: refreshToken });
      return errJson(res, 400, "TOKEN_EXPIRED");
    }
    // Xoay vòng refresh token (revoke cái cũ, cấp cái mới)
    await db.collection("refresh_tokens").deleteOne({ token: refreshToken });
    return res.json(await issueCreds({ uid: rec.uid, email: rec.email }));
  } catch (e) {
    console.error("[auth] refresh error", e);
    return errJson(res, 500, "INTERNAL");
  }
});

// Đổi mật khẩu (cần Authorization: Bearer)
router.post("/update-password", authRequired, async (req, res) => {
  try {
    const password = String(req.body.password || "");
    if (password.length < 6) return errJson(res, 400, "WEAK_PASSWORD");
    const db = getDb();
    const passwordHash = await bcrypt.hash(password, 10);
    await db
      .collection("auth_users")
      .updateOne({ uid: req.auth.uid }, { $set: { passwordHash } });
    // Thu hồi mọi refresh token cũ của user rồi cấp creds mới.
    await db.collection("refresh_tokens").deleteMany({ uid: req.auth.uid });
    return res.json(await issueCreds({ uid: req.auth.uid, email: req.auth.email }));
  } catch (e) {
    console.error("[auth] update-password error", e);
    return errJson(res, 500, "INTERNAL");
  }
});

// Reset mật khẩu — số quân map sang email ảo (@ll47.local) nên không gửi mail
// thật được. Trả ok để tương thích; reset thực tế do admin thực hiện trong app.
router.post("/reset", async (req, res) => {
  return res.json({ ok: true, note: "Reset email không khả dụng với email nội bộ. Liên hệ admin." });
});

// Lấy thông tin tài khoản theo idToken
router.post("/lookup", async (req, res) => {
  try {
    let token = String(req.body.idToken || "");
    if (!token) {
      const h = req.headers.authorization || "";
      const m = h.match(/^Bearer\s+(.+)$/i);
      if (m) token = m[1];
    }
    if (!token) return errJson(res, 401, "MISSING_ID_TOKEN");
    let payload;
    try {
      payload = verifyIdToken(token);
    } catch (e) {
      return errJson(res, 401, "INVALID_ID_TOKEN");
    }
    const db = getDb();
    const user = await db.collection("auth_users").findOne({ uid: payload.uid });
    if (!user) return errJson(res, 400, "USER_NOT_FOUND");
    return res.json({
      users: [
        {
          localId: user.uid,
          email: user.email,
          emailVerified: false,
          disabled: !!user.disabled,
        },
      ],
    });
  } catch (e) {
    console.error("[auth] lookup error", e);
    return errJson(res, 500, "INTERNAL");
  }
});

module.exports = { router, authRequired, verifyIdToken };
