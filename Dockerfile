FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa tesseract-ocr-fra \
    tesseract-ocr-deu tesseract-ocr-ita tesseract-ocr-por tesseract-ocr-nld \
    libtesseract-dev libleptonica-dev libglib2.0-0 libsm6 libxrender1 libxext6 libgl1 \
    ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py /app/server.py

ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata
ENV PORT=8000
EXPOSE 8000

CMD ["gunicorn","server:app","-b","0.0.0.0:8000","--timeout","120"]
