"use strict";
/**
 * Kho document tổng quát theo "path" kiểu Firestore, lưu trong 1 collection
 * MongoDB tên `documents`. Nhờ vậy client cũ (vốn gọi theo path Firestore)
 * gần như không phải đổi gì.
 *
 *   GET    /doc/<path>           -> get_doc   (path là document: số segment chẵn)
 *   PATCH  /doc/<path>           -> set_doc   body { data, merge }
 *   DELETE /doc/<path>           -> delete_doc (xoá kèm subcollection)
 *   GET    /collection/<path>    -> list_collection (path là collection: số segment lẻ)
 *   POST   /query/<collection>   -> query     body { where:[[f,op,v]], orderBy, limit }
 */
const express = require("express");
const { getDb } = require("./db");
const realtime = require("./realtime");

const router = express.Router();

function segOf(path) {
  return String(path || "").split("/").filter(Boolean);
}
function parentOf(segments) {
  return segments.slice(0, -1).join("/");
}
function escapeRegex(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function decodePath(req) {
  try {
    return decodeURIComponent(req.params[0] || "");
  } catch (e) {
    return req.params[0] || "";
  }
}
function rowToClient(row) {
  const out = Object.assign({}, row.data || {});
  out._id = row._id;
  out._updateTime = row.updatedAt ? new Date(row.updatedAt).toISOString() : "";
  return out;
}

// ---- GET document ----
router.get(/^\/doc\/(.+)/, async (req, res) => {
  const path = decodePath(req);
  const segs = segOf(path);
  if (segs.length === 0) {
    return res.status(400).json({ error: { message: "INVALID_DOCUMENT_PATH" } });
  }
  const row = await getDb().collection("documents").findOne({ _path: path });
  if (!row) return res.status(404).json({ error: { message: "NOT_FOUND" } });
  return res.json(row.data || {});
});

// ---- PATCH (set/merge) document ----
router.patch(/^\/doc\/(.+)/, async (req, res) => {
  const path = decodePath(req);
  const segs = segOf(path);
  if (segs.length === 0) {
    return res.status(400).json({ error: { message: "INVALID_DOCUMENT_PATH" } });
  }
  const data = (req.body && req.body.data) || {};
  const merge = req.body && req.body.merge !== false; // mặc định merge=true
  const parent = parentOf(segs);
  const id = segs[segs.length - 1];
  const now = new Date();
  const col = getDb().collection("documents");

  if (merge) {
    const $set = { _path: path, _parent: parent, _id: id, updatedAt: now };
    for (const [k, v] of Object.entries(data)) $set[`data.${k}`] = v;
    await col.updateOne({ _path: path }, { $set }, { upsert: true });
  } else {
    await col.updateOne(
      { _path: path },
      { $set: { _path: path, _parent: parent, _id: id, data, updatedAt: now } },
      { upsert: true }
    );
  }

  realtime.emitChange(parent);
  const row = await col.findOne({ _path: path });
  return res.json(row ? row.data || {} : {});
});

// ---- DELETE document (+ subcollection) ----
router.delete(/^\/doc\/(.+)/, async (req, res) => {
  const path = decodePath(req);
  const segs = segOf(path);
  if (segs.length === 0) {
    return res.status(400).json({ error: { message: "INVALID_DOCUMENT_PATH" } });
  }
  const parent = parentOf(segs);
  const col = getDb().collection("documents");
  await col.deleteOne({ _path: path });
  // Xoá đệ quy mọi document nằm dưới path này (subcollection).
  await col.deleteMany({ _path: { $regex: "^" + escapeRegex(path) + "/" } });
  realtime.emitChange(parent);
  return res.json({ ok: true });
});

// ---- LIST collection ----
router.get(/^\/collection\/(.+)/, async (req, res) => {
  const path = decodePath(req);
  const segs = segOf(path);
  if (segs.length === 0) {
    return res.status(400).json({ error: { message: "INVALID_COLLECTION_PATH" } });
  }
  const pageSize = Math.min(parseInt(req.query.pageSize || "1000", 10) || 1000, 5000);
  const rows = await getDb()
    .collection("documents")
    .find({ _parent: path })
    .limit(pageSize)
    .toArray();
  return res.json(rows.map(rowToClient));
});

// ---- QUERY collection ----
const OP_MAP = {
  EQUAL: "$eq",
  NOT_EQUAL: "$ne",
  LESS_THAN: "$lt",
  LESS_THAN_OR_EQUAL: "$lte",
  GREATER_THAN: "$gt",
  GREATER_THAN_OR_EQUAL: "$gte",
  ARRAY_CONTAINS: "$eq", // Mongo: {field: v} khớp nếu mảng chứa v
  IN: "$in",
  ARRAY_CONTAINS_ANY: "$in",
};

router.post(/^\/query\/(.+)/, async (req, res) => {
  const collection = decodePath(req);
  const segs = segOf(collection);
  if (segs.length === 0 || segs.length % 2 !== 1) {
    return res.status(400).json({ error: { message: "INVALID_COLLECTION_PATH" } });
  }
  const where = (req.body && req.body.where) || [];
  const orderBy = req.body && req.body.orderBy;
  const limit = req.body && parseInt(req.body.limit, 10);

  const filter = { _parent: collection };
  for (const cond of where) {
    if (!Array.isArray(cond) || cond.length !== 3) continue;
    const [field, op, value] = cond;
    const mop = OP_MAP[op] || "$eq";
    filter[`data.${field}`] = { [mop]: value };
  }

  let cursor = getDb().collection("documents").find(filter);
  if (orderBy) cursor = cursor.sort({ [`data.${orderBy}`]: 1 });
  if (limit && limit > 0) cursor = cursor.limit(limit);
  const rows = await cursor.toArray();
  return res.json(rows.map(rowToClient));
});

module.exports = router;
