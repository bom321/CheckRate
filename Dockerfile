# CheckRate — Deposit Rate Monitor + Dashboard
# Base: python:3.13-slim, ทำงานบน Synology DS916+ (Intel x86_64)

FROM python:3.13-slim

# curl: จำเป็นสำหรับ download_pdf() ใน app/monitor/common.py
# tzdata: ตั้ง timezone Asia/Bangkok
# tesseract-ocr + tesseract-ocr-tha: PDF ประกาศของ BBL เป็นภาพสแกน (ไม่มี text layer)
#   banks/bbl.py จึงต้อง OCR ภาษาไทย — ถ้าไม่มี package นี้ BBL จะอ่านอัตราไม่ได้ (ธนาคารอื่นไม่กระทบ)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tzdata \
        tesseract-ocr \
        tesseract-ocr-tha \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Bangkok \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

# supercronic — cron ที่ทำงานใน foreground/container ได้ดีกว่า cron ปกติ
# (บันทึกไว้ตรงนี้ ถ้าจะอัปเดตเวอร์ชันในอนาคตให้แก้ URL + SHA1SUM ตาม release ใหม่)
ARG SUPERCRONIC_VERSION=v0.2.29
ARG SUPERCRONIC=supercronic-linux-amd64
ARG SUPERCRONIC_SHA1SUM=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b
RUN curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${SUPERCRONIC}" \
    && echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - \
    && chmod +x "${SUPERCRONIC}" \
    && mv "${SUPERCRONIC}" /usr/local/bin/supercronic

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY crontab ./crontab
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
