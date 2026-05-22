"use strict";
/**
 * File storage qua GridFS — thay Firebase Storage.
 *
 *   POST   /storage/upload?path=<remote/path>   (raw body, Content-Type = loai file)
 *          -> { name, downloadURL, token, size, contentType }
 *   GET    /storage/o/<encoded-path>?token=...   (CONG KHAI) — dung trong downloadURL
 *   GET    /storage/file/<path>?token=...        (CONG KHAI) — truy cap truc tiep theo path
 *   DELETE /storage/file/<path>                  (can Authorization)
 *
 * downloadURL co tinh chua "/o/" + path encode-1-lan de tuong thich voi code don
 * file cu trong app (von tach path bang split("/o/")). Nho vay KHONG phai sua main.py.
 */
const crypto = require("crypto");
const express = require("express");
const { getBucket } = require("./db");

const publicRouter = express.Router(); // GET file — khong can auth
const router = express.Router(); // upload/delete — gan authRequired o index.js

const MAX_UPLOAD_MB = parseInt(process.env.MAX_UPLOAD_MB || "25", 10) || 25;
const rawBody = express.raw({ type: () => true, limit: `${MAX_UPLOAD_MB}mb` });

function decodeWildcard(req) {
  try {
    return decodeURIComponent(req.params[0] || "");
  } catch (e) {
    return req.params[0] || "";
  }
}
function publicUrl() {
  return (process.env.PUBLIC_URL || `http://localhost:${process.env.PORT || 8080}`).replace(/\/+$/, "");
}

async function deleteByName(bucket, filename) {
  const files = await bucket.find({ filename }).toArray();
  for (const f of files) {
    try {
      await bucket.delete(f._id);
    } catch (e) {
      /* bo qua */
    }
  }
  return files.length;
}

async function streamFile(req, res, filename) {
  const bucket = getBucket();
  const files = await bucket.find({ filename }).limit(1).toArray();
  if (!files.length) return res.status(404).json({ error: { message: "NOT_FOUND" } });
  const file = files[0];
  const meta = file.metadata || {};
  if (meta.token && req.query.token && req.query.token !== meta.token) {
    return res.status(403).json({ error: { message: "INVALID_TOKEN" } });
  }
  res.set("Content-Type", meta.contentType || "application/octet-stream");
  if (file.length) res.set("Content-Length", String(file.length));
  res.set("Cache-Control", "public, max-age=86400");
  const stream = bucket.openDownloadStreamByName(filename);
  stream.on("error", () => {
    if (!res.headersSent) res.status(404).end();
  });
  stream.pipe(res);
}

// ---- Upload (raw body) ----
router.post("/upload", rawBody, async (req, res) => {
  try {
    const path = String(req.query.path || req.query.name || "");
    if (!path) return res.status(400).json({ error: { message: "MISSING_PATH" } });
    const buf = req.body;
    if (!Buffer.isBuffer(buf) || buf.length === 0) {
      return res.status(400).json({ error: { message: "EMPTY_BODY" } });
    }
    const contentType = req.headers["content-type"] || "application/octet-stream";
    const token = crypto.randomBytes(16).toString("hex");
    const bucket = getBucket();

    await deleteByName(bucket, path); // ghi de ban cu

    await new Promise((resolve, reject) => {
      const up = bucket.openUploadStream(path, {
        metadata: { contentType, token, uploadedAt: new Date() },
      });
      up.on("error", reject);
      up.on("finish", resolve);
      up.end(buf);
    });

    const downloadURL = `${publicUrl()}/storage/o/${encodeURIComponent(path)}?alt=media&token=${token}`;
    return res.json({ name: path, downloadURL, token, size: buf.length, contentType });
  } catch (e) {
    console.error("[storage] upload error", e);
    return res.status(500).json({ error: { message: "UPLOAD_FAILED" } });
  }
});

// ---- Download cong khai: /storage/o/<encoded-path> (path encode 1 lan) ----
publicRouter.get(/^\/storage\/o\/(.+)/, (req, res) => {
  streamFile(req, res, decodeWildcard(req)).catch((e) => {
    console.error("[storage] download(o) error", e);
    if (!res.headersSent) res.status(500).json({ error: { message: "DOWNLOAD_FAILED" } });
  });
});

// ---- Download cong khai: /storage/file/<path> (slash giu nguyen) ----
publicRouter.get(/^\/storage\/file\/(.+)/, (req, res) => {
  streamFile(req, res, decodeWildcard(req)).catch((e) => {
    console.error("[storage] download(file) error", e);
    if (!res.headersSent) res.status(500).json({ error: { message: "DOWNLOAD_FAILED" } });
  });
});

// ---- Delete (can auth) — mount tai /storage ----
router.delete(/^\/file\/(.+)/, async (req, res) => {
  try {
    const n = await deleteByName(getBucket(), decodeWildcard(req));
    return res.json({ ok: true, deleted: n });
  } catch (e) {
    console.error("[storage] delete error", e);
    return res.status(500).json({ error: { message: "DELETE_FAILED" } });
  }
});

module.exports = { publicRouter, router };
