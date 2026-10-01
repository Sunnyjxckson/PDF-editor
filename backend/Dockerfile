FROM python:3.11-slim

WORKDIR /app

# System dependencies: gcc for wheels that build from source, Tesseract + English
# tessdata (plus common language packs) for OCR (convert feature), libglib2.0-0 for opencv-python-headless
# (pulled in by pdf2docx).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-spa tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-ita \
    tesseract-ocr-por tesseract-ocr-nld tesseract-ocr-rus tesseract-ocr-ara \
    tesseract-ocr-hin tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
    tesseract-ocr-jpn tesseract-ocr-kor \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Languages installed from the UI land here; mount a volume to keep them.
ENV PDF_EDITOR_TESSDATA_DIR=/app/backend/tessdata
RUN mkdir -p /app/backend/tessdata

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
