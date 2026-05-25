FROM node:22-alpine

WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN npm ci --omit=dev

# Copy source and frontend
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY tsconfig.json ./

# Create data directory (volume will be mounted here)
RUN mkdir -p /data

EXPOSE 5173

CMD ["npx", "tsx", "src/index.ts"]
