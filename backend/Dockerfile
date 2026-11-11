FROM python:3.12-slim

# Tesseract OCR for scanned invoices
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# demo invoices (served on the Upload page)
RUN python scripts/generate_samples.py samples

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
