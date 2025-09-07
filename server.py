from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ExifTags, ImageOps, ImageFilter
from datetime import date
from functools import wraps
import io, os, traceback, subprocess, pytesseract, re

app = Flask(__name__)
CORS(app, resources={ r"/mrz": { "origins": [
    "https://pmsopalmo.campingsopalmo.com",
    "https://campingsopalmo.com",
]}})

API_KEY = os.environ.get("MRZ_API_KEY", "CAMBIA_ESTA_CLAVE")
TESSDATA_DIRS = [
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tesseract-ocr/5/tessdata",
]

def require_api_key(f):
    @wraps(f)
    def wrap(*a, **k):
        if API_KEY and request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*a, **k)
    return wrap

@app.get("/")
def index():
    return {"ok": True, "service": "mrz", "tip": "POST /mrz?fast=1 con X-API-Key"}, 200

@app.get("/health")
def health():
    return {"ok": True, "service": "mrz", "has_ocrb": ("ocrb" in [l.lower() for l in list_tess_langs()])}, 200

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
        if r.returncode != 0: return []
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        return [ln for ln in lines if ln and not ln.lower().startswith("list of")]
    except Exception:
        return []

def normalize_date(yyMMdd: str | None):
    if not yyMMdd or len(yyMMdd) != 6: return None
    yy = int(yyMMdd[:2]); mm = yyMMdd[2:4]; dd = yyMMdd[4:6]
    nowyy = date.today().year % 100
    century = 2000 if yy <= nowyy else 1900
    return f"{century + yy}-{mm}-{dd}"

def fix_orientation(raw_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw_bytes))
    try:
        orientation_tag = next((k for k,v in ExifTags.TAGS.items() if v=="Orientation"), None)
        exif = img._getexif() if hasattr(img, "_getexif") else None
        if exif and orientation_tag in exif:
            o = exif[orientation_tag]
            if o == 3:   img = img.rotate(180, expand=True)
            elif o == 6: img = img.rotate(270, expand=True)
            elif o == 8: img = img.rotate(90, expand=True)
    except Exception:
        pass
    return img

def to_jpeg_bytes(img: Image.Image, quality: int = 92) -> bytes:
    out = io.BytesIO(); img.save(out, format="JPEG", quality=quality); out.seek(0); return out.getvalue()

def enhance_for_mrz(img: Image.Image) -> Image.Image:
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.SHARPEN)
    return g

def sanitize_line(s: str) -> str:
    return re.sub(r"[^A-Z0-9<]", "", (s or "").upper())

def normalize_td1_line(line: str) -> str:
    s = sanitize_line(line)
    if len(s) > 30: s = s[:30]
    if len(s) < 30: s = s + "<" * (30 - len(s))
    return s

def crop_bottom_band(img: Image.Image, top_frac=0.60, bottom_frac=0.98, margin_x=0.03) -> Image.Image:
    w, h = img.size
    x0 = int(w*margin_x); x1 = int(w*(1-margin_x))
    y0 = int(h*top_frac); y1 = int(h*bottom_frac)
    crop = img.crop((x0, y0, x1, y1))
    # escalar a ancho ~1200px para rapidez
    target_w = 1200
    if crop.width > target_w:
        scale = target_w / crop.width
        crop = crop.resize((int(crop.width*scale), int(crop.height*scale)), Image.BICUBIC)
    return enhance_for_mrz(crop)

def ocr_bottom_mrz_lines_fast(img: Image.Image):
    crop = crop_bottom_band(img)
    cfg = "--oem 3 --psm 6 -l ocrb -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
    txt = pytesseract.image_to_string(crop, config=cfg) or ""
    lines = [sanitize_line(l) for l in txt.splitlines() if l.strip()]
    if len(lines) > 3: lines = lines[-3:]
    lines = [normalize_td1_line(l) for l in lines]  # 30 chars
    return lines, txt

def try_read_mrz(bytes_jpg: bytes, psm_list=(6,7,11)):
    params = [f"--oem 3 --psm {p} -l ocrb" for p in psm_list]
    for p in params:
        mrz = read_mrz(io.BytesIO(bytes_jpg), save_roi=True, extra_cmdline_params=p)
        if mrz is not None:
            return mrz
    # rotado 180
    img = Image.open(io.BytesIO(bytes_jpg))
    rot = img.rotate(180, expand=True)
    rb = to_jpeg_bytes(rot, 92)
    for p in params:
        mrz = read_mrz(io.BytesIO(rb), save_roi=True, extra_cmdline_params=p)
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

        img = fix_orientation(f.read())
        fast = (request.args.get("fast", "1") == "1")

        # ====== MODO RÁPIDO: solo OCR de franja MRZ ======
        if fast:
            lines, ocr_raw = ocr_bottom_mrz_lines_fast(img)
            raw = "\n".join(lines)
            optional_td1 = ""
            if lines:
                l1 = lines[0]
                optional_td1 = l1[15:30].replace("<","")
            return jsonify({
                "ok": True,
                "type": "TD1",                   # asumimos ID en modo rápido (la mayoría de DNIs)
                "doc_code": "I",
                "issuing_country": "",           # vacío en rápido
                "numero": "",                    # vacío en rápido
                "nacionalidad": "",
                "apellidos": "",
                "nombres": "",
                "sexo": "",
                "nacimiento": None,
                "expiracion": None,
                "optional": "",
                "raw": "",                       # PassportEye no corrido
                "raw_ocr": ocr_raw,
                "mrz_lines": lines,
                "optional_td1": optional_td1,
            })

        # ====== MODO COMPLETO (más lento): PassportEye + fallback OCR ======
        mrz_obj = try_read_mrz(to_jpeg_bytes(img, 92), psm_list=(6,7,11))
        d = mrz_obj.to_dict() if mrz_obj else {}
        raw_pe = d.get("mrz_text") or ""

        lines, ocr_raw = ([], "")
        if not raw_pe:
            lines, ocr_raw = ocr_bottom_mrz_lines_fast(img)

        raw = raw_pe or "\n".join(lines)
        optional_td1 = ""
        if raw:
            l1 = raw.splitlines()[0] if "\n" in raw else raw
            l1 = normalize_td1_line(l1)
            optional_td1 = l1[15:30].replace("<","")

        return jsonify({
            "ok": True,
            "type": d.get("mrz_type"),
            "doc_code": d.get("type"),
            "issuing_country": d.get("country"),
            "numero": d.get("number"),
            "nacionalidad": (d.get("nationality") or "").upper(),
            "apellidos": d.get("surname"),
            "nombres": d.get("names"),
            "sexo": (d.get("sex") or "").upper(),
            "nacimiento": normalize_date(d.get("date_of_birth")),
            "expiracion": normalize_date(d.get("expiration_date")),
            "optional": (d.get("personal_number") or d.get("optional_data") or ""),
            "raw": raw_pe,
            "raw_ocr": ocr_raw or raw,
            "mrz_lines": lines,
            "optional_td1": optional_td1,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500
