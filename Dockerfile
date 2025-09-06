FROM python:3.11-slim

# 1) Paquetes del sistema + Tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng libtesseract-dev libleptonica-dev \
    libglib2.0-0 libsm6 libxrender1 libxext6 libgl1 \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2) Dependencias Python (wheels precompiladas para evitar builds)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 3) Código
COPY server.py /app/server.py

# 4) Modelo OCRB -> tessdata (imprescindible)
#   Asegúrate de tener este fichero en tu repo: tess/ocrb.traineddata
COPY tess/ocrb.traineddata /usr/share/tesseract-ocr/4.00/tessdata/ocrb.traineddata
RUN mkdir -p /usr/share/tesseract-ocr/5/tessdata && \
    cp /usr/share/tesseract-ocr/4.00/tessdata/ocrb.traineddata /usr/share/tesseract-ocr/5/tessdata/ocrb.traineddata

ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata
ENV PORT=8000
EXPOSE 8000

CMD ["gunicorn", "server:app", "-b", "0.0.0.0:8000", "--timeout", "120"]
