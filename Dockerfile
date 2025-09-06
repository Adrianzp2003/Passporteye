# Imagen base
FROM python:3.11-slim

# Paquetes del sistema + Tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr libtesseract-dev libleptonica-dev ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Carpeta de la app
WORKDIR /app

# Dependencias Python
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el código
COPY server.py /app/server.py

# Copiamos el modelo OCRB (imprescindible para MRZ)
# Asegúrate de tener /tess/ocrb.traineddata en tu repo
COPY tess/ocrb.traineddata /usr/share/tesseract-ocr/4.00/tessdata/ocrb.traineddata
# Por si la distro usa el path de Tesseract 5:
RUN mkdir -p /usr/share/tesseract-ocr/5/tessdata && \
    cp /usr/share/tesseract-ocr/4.00/tessdata/ocrb.traineddata /usr/share/tesseract-ocr/5/tessdata/ocrb.traineddata

# (Opcional) variable por si Tesseract la necesita
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata

# Puerto para Render
ENV PORT=8000
EXPOSE 8000

# Arranque
CMD ["gunicorn", "server:app", "-b", "0.0.0.0:8000", "--timeout", "120"]
