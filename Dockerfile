FROM python:3.12-slim

WORKDIR /app

# Install Playwright system dependencies manually (avoids ttf-unifont issue)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    libdbus-1-3 libexpat1 libfontconfig1 libgcc-s1 libglib2.0-0 \
    libstdc++6 fonts-liberation fonts-noto-color-emoji \
    ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY backend/ backend/
COPY frontend/ frontend/

EXPOSE 8199

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8199"]
