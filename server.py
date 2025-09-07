from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ExifTags, ImageOps, ImageFilter
from datetime import date
from functools import wraps
import io
import os
import traceback
import subprocess

app = Flask(__name__)

# ==== CORS: AJUSTA a tus dominios ====
CORS(
    app,
    resources={
        r"/mrz": {
            "origins": [
                "https://pmsopalmo.campingsopalmo.com",
                "https://campingsopalmo.com",
            ]
        }
    },
)

# ==== API KEY: la misma en Render (Settings → Environment) ====
API_KEY = os.environ.get("MRZ_API_KEY", "CAMBIA_ESTA_CLAVE")

# ==== Posibles rutas de tessdata (Dockerfile copia ocrb aquí) ====
TESSDATA_DIRS = [
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tesseract-ocr/5/tessdata",
]

def require_api_key(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if API_KEY and request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrap

@app.get("/")
def index():
    return {"ok": True, "service": "mrz", "tip": "POST /mrz con X-API-Key"}, 200

@app.get("/health")
def health():
    langs = list_tess_langs()
    return {
        "ok": True,
        "service": "mrz",
        "has_ocrb": ("ocrb" in [l.lower() for l in langs]),
    }, 200

@app.get("/diag")
def diag():
    langs = list_tess_langs()
    exists = {d: os.path.isfile(os.path.join(d, "ocrb.traineddata")) for d in TESSDATA_DIRS}
    return {
        "ok": True,
        "tesseract_version": get_tesseract_version(),
        "tessdata_prefix": os.environ.get("TESSDATA_PREFIX"),
        "tessdata_dirs": TESSDATA_DIRS,
        "ocrb_present_in_dirs": exists,
        "langs": langs,
        "has_ocrb": ("ocrb" in [l.lower() for l in langs]),
        "tip": "Debe aparecer 'ocrb' en langs y al menos un *.traineddata en las rutas",
    }, 200

@app.route("/mrz", methods=["OPTIONS"])
def mrz_options():
    return make_response(("", 204))

def get_tesseract_version():
    try:
        r = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=5)
        return r.stdout.splitlines()[0] if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

def list_tess_langs():
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        return [ln for ln in lines if ln and not ln.lower().startswith("list of")]
    except Exception:
        return []

def normalize_date(yyMMdd: str | None):
    if not yyMMdd or len(yyMMdd) != 6:
        return None
    yy = int(yyMMdd[:2]); mm = yyMMdd[2:4]; dd = yyMMdd[4:6]
    nowyy = date.today().year % 100
    century = 2000 if yy <= nowyy else 1900
    return f"{century + yy}-{mm}-{dd}"

def fix_orientation(raw_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw_bytes))
    try:
        orientation_tag = None
        for k, v in ExifTags.TAGS.items():
            if v == "Orientation":
                orientation_tag = k
                break
        exif = img._getexif() if hasattr(img, "_getexif") else None
        if exif and orientation_tag in exif:
            o = exif[orientation_tag]
            if o == 3:   img = img.rotate(180, expand=True)
            elif o == 6: img = img.rotate(270, expand=True)
            elif o == 8: img = img.rotate(90, expand=True)
    except Exception:
        pass
    return img

def to_jpeg_bytes(img: Image.Image, quality: int = 95) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    out.seek(0)
    return out.getvalue()

def enhance_for_mrz(img: Image.Image) -> Image.Image:
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.SHARPEN)
    return g

def try_read_mrz(img_bytes: bytes, psm_list=(6, 7, 11)):
    """
    Bucle de intentos con distintos PSMs y rotación 180º.
    (PassportEye 2.2.2 no soporta 'force_rectify', así que no lo usamos.)
    """
    param_list = [f"--oem 3 --psm {p} -l ocrb" for p in psm_list]

    # Normal
    for params in param_list:
        mrz = read_mrz(io.BytesIO(img_bytes), save_roi=True, extra_cmdline_params=params)
        if mrz is not None:
            return mrz

    # Rotado 180º
    img = Image.open(io.BytesIO(img_bytes))
    rot = img.rotate(180, expand=True)
    rot_b = to_jpeg_bytes(rot, 95)
    for params in param_list:
        mrz = read_mrz(io.BytesIO(rot_b), save_roi=True, extra_cmdline_params=params)
        if mrz is not None:
            return mrz

    return None

def try_read_mrz_with_crops(img: Image.Image):
    """
    Reintentos sobre recortes de la franja inferior (útil para DNI TD1),
    con reescalado y mejora de contraste/enfoque.
    """
    w, h = img.size
    bands = [
        (0.50, 0.98),
        (0.58, 0.98),
        (0.65, 0.98),
    ]
    for (y0f, y1f) in bands:
        y0 = int(h * y0f); y1 = int(h * y1f)
        crop = img.crop((int(w * 0.03), y0, int(w * 0.97), y1))
        scale = 1.6
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.BICUBIC)
        crop2 = enhance_for_mrz(crop)
        b = to_jpeg_bytes(crop2, 95)
        mrz = try_read_mrz(b, psm_list=(6, 7, 11))
        if mrz is not None:
            return mrz
    return None

@app.post("/mrz")
@require_api_key
def mrz():
    try:
        f = request.files.get("image")
        if not f:
            return jsonify({"ok": False, "error": 'No file "image"'}), 400

        # 1) Orientación correcta
        img = fix_orientation(f.read())

        # 2) Intento directo (pasaporte TD3 y muchos TD1)
        mrz_obj = try_read_mrz(to_jpeg_bytes(img, 95), psm_list=(6, 7, 11))
        if mrz_obj is None:
            # 3) Reintentos en franja inferior (DNI TD1)
            mrz_obj = try_read_mrz_with_crops(img)

        if mrz_obj is None:
            return jsonify({"ok": False, "error": "MRZ no detectada"}), 422

        d = mrz_obj.to_dict()
        return jsonify({
            "ok": True,
            "type": d.get("mrz_type"),               # TD1 o TD3
            "doc_code": d.get("type"),               # P/I (pasaporte/ID card)
            "issuing_country": d.get("country"),     # país emisor (ej. ESP)
            "numero": d.get("number"),
            "nacionalidad": (d.get("nationality") or "").upper(),
            "apellidos": d.get("surname"),
            "nombres": d.get("names"),
            "sexo": (d.get("sex") or "").upper(),    # M/F/X
            "nacimiento": normalize_date(d.get("date_of_birth")),
            "expiracion": normalize_date(d.get("expiration_date")),
            "optional": (d.get("personal_number") or d.get("optional_data") or ""),
            "raw": d.get("mrz_text"),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"EXCEPTION: {str(e)}",
            "trace": traceback.format_exc()[:2000],
        }), 500
