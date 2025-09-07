from flask import Flask, request, jsonify
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ExifTags, ImageOps, ImageFilter
from datetime import date
from functools import wraps
import io
import os

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

# ==== API KEY: usa la misma en Render (Settings → Environment) ====
API_KEY = os.environ.get("MRZ_API_KEY", "pirulico22")


def require_api_key(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if API_KEY and request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return wrap


@app.get("/health")
def health():
    return {"ok": True, "service": "mrz"}, 200


def normalize_date(yyMMdd: str | None):
    if not yyMMdd or len(yyMMdd) != 6:
        return None
    yy = int(yyMMdd[:2])
    mm = yyMMdd[2:4]
    dd = yyMMdd[4:6]
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
            if o == 3:
                img = img.rotate(180, expand=True)
            elif o == 6:
                img = img.rotate(270, expand=True)
            elif o == 8:
                img = img.rotate(90, expand=True)
    except Exception:
        pass
    return img


def to_jpeg_bytes(img: Image.Image, quality: int = 95) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    out.seek(0)
    return out.getvalue()


def enhance_for_mrz(img: Image.Image) -> Image.Image:
    # boost para MRZ pequeña (DNI TD1)
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.SHARPEN)
    return g


def try_read_mrz(img_bytes: bytes, psm_list=(6, 7, 11), rectify_first: bool = False):
    tries = []
    for p in psm_list:
        tries.append(
            {"force_rectify": False, "params": f"--oem 3 --psm {p} -l ocrb"}
        )
    for p in psm_list:
        tries.append(
            {"force_rectify": True, "params": f"--oem 3 --psm {p} -l ocrb"}
        )

    if rectify_first:
        tries = sorted(tries, key=lambda t: not t["force_rectify"])

    # normal
    for t in tries:
        mrz = read_mrz(
            io.BytesIO(img_bytes),
            save_roi=True,
            force_rectify=t["force_rectify"],
            extra_cmdline_params=t["params"],
        )
        if mrz is not None:
            return mrz

    # rotado 180º
    img = Image.open(io.BytesIO(img_bytes))
    rot = img.rotate(180, expand=True)
    rot_b = to_jpeg_bytes(rot, 95)
    for t in tries:
        mrz = read_mrz(
            io.BytesIO(rot_b),
            save_roi=True,
            force_rectify=t["force_rectify"],
            extra_cmdline_params=t["params"],
        )
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
        y0 = int(h * y0f)
        y1 = int(h * y1f)
        crop = img.crop((int(w * 0.03), y0, int(w * 0.97), y1))
        # escala
        scale = 1.6
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.BICUBIC)
        crop2 = enhance_for_mrz(crop)
        b = to_jpeg_bytes(crop2, 95)
        mrz = try_read_mrz(b, psm_list=(6, 7, 11), rectify_first=True)
        if mrz is not None:
            return mrz
    return None


@app.post("/mrz")
@require_api_key
def mrz():
    f = request.files.get("image")
    if not f:
        return jsonify({"ok": False, "error": 'No file "image"'}), 400

    # 1) Orientación correcta
    img = fix_orientation(f.read())

    # 2) Intento directo (bueno para pasaporte TD3 y muchos TD1)
    mrz_obj = try_read_mrz(to_jpeg_bytes(img, 95), psm_list=(6, 7, 11))
    if mrz_obj is None:
        # 3) Reintentos apuntando a franja inferior (DNI TD1) con mejora
        mrz_obj = try_read_mrz_with_crops(img)

    if mrz_obj is None:
        return jsonify({"ok": False, "error": "MRZ no detectada"}), 422

    d = mrz_obj.to_dict()

    # === RESPUESTA EXTENDIDA (sin tabs, 4 espacios) ===
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

