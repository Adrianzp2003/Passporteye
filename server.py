from flask import Flask, request, jsonify
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ExifTags
import io
from datetime import date

app = Flask(__name__)
CORS(app)  # limita luego a tu dominio si quieres

@app.get("/health")
def health():
    return {"ok": True, "service": "mrz"}, 200

def normalize_date(yyMMdd):
    if not yyMMdd or len(yyMMdd) != 6:
        return None
    yy = int(yyMMdd[:2])
    mm = yyMMdd[2:4]
    dd = yyMMdd[4:6]
    nowyy = date.today().year % 100
    century = 2000 if yy <= nowyy else 1900
    return f"{century+yy}-{mm}-{dd}"

def read_image_fix_orientation(raw_bytes):
    img = Image.open(io.BytesIO(raw_bytes))
    try:
        for orientation in ExifTags.TAGS:
            if ExifTags.TAGS[orientation] == 'Orientation':
                break
        exif = img._getexif()
        if exif and orientation in exif:
            o = exif[orientation]
            if o == 3:   img = img.rotate(180, expand=True)
            elif o == 6: img = img.rotate(270, expand=True)
            elif o == 8: img = img.rotate(90, expand=True)
    except Exception:
        pass
    out = io.BytesIO()
    img.save(out, format='JPEG', quality=95)
    out.seek(0)
    return out.getvalue()

@app.post("/mrz")
def mrz():
    f = request.files.get('image')
    if not f:
        return jsonify({'ok': False, 'error': 'No file "image"'}), 400

    fixed = read_image_fix_orientation(f.read())

    mrz = read_mrz(io.BytesIO(fixed), save_roi=True, extra_cmdline_params='--oem 3 --psm 6')
    if mrz is None:
        mrz = read_mrz(io.BytesIO(fixed), save_roi=True, force_rectify=True)
    if mrz is None:
        return jsonify({'ok': False, 'error': 'MRZ no detectada'}), 422

    d = mrz.to_dict()
    return jsonify({
        'ok': True,
        'type': d.get('mrz_type'),
        'numero': d.get('number'),
        'nacionalidad': (d.get('nationality') or '').upper(),
        'apellidos': d.get('surname'),
        'nombres': d.get('names'),
        'sexo': (d.get('sex') or '').upper(),
        'nacimiento': normalize_date(d.get('date_of_birth')),
        'expiracion': normalize_date(d.get('expiration_date')),
        'raw': d.get('mrz_text')
    })
