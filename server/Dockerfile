# Backend LL47 — Node.js + Express + Socket.io + MongoDB (GridFS)
FROM node:20-alpine

WORKDIR /app

# Cài dependency trước để tận dụng layer cache
COPY package*.json ./
RUN npm install --omit=dev

# Copy mã nguồn
COPY . .

ENV NODE_ENV=production
EXPOSE 8080

CMD ["node", "src/index.js"]
