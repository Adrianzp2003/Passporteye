from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ExifTags, ImageOps, ImageFilter
from datetime import date
from functools import wraps
import io, os, traceback, subprocess, pytesseract, re, unicodedata

app = Flask(__name__)
CORS(app, resources={ r"/": {"origins": "*"}, r"/mrz": {"origins": "*"}, r"/idocr": {"origins": "*"}})

API_KEY = os.environ.get("MRZ_API_KEY", "CAMBIA_ESTA_CLAVE")

def require_api_key(f):
    @wraps(f)
    def wrap(*a, **k):
        if API_KEY and request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*a, **k)
    return wrap

@app.get("/")
def index():
    return {"ok": True, "service": "mrz+idocr", "tip": "POST /idocr (front, back) y /mrz"}, 200

@app.get("/health")
def health():
    return {"ok": True, "tesseract": get_tesseract_version()}, 200

@app.route("/mrz", methods=["OPTIONS"])
@app.route("/idocr", methods=["OPTIONS"])
def options_any():
    return make_response(("", 204))

# ---------- Utilidades comunes ----------

def get_tesseract_version():
    try:
        r = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=5)
        return r.stdout.splitlines()[0] if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

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

def enhance_text(img: Image.Image) -> Image.Image:
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.SHARPEN)
    return g

def to_jpeg_bytes(img: Image.Image, quality: int = 92) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    out.seek(0)
    return out.getvalue()

def ocr_image(img: Image.Image, lang="spa+eng", psm=6) -> str:
    proc = enhance_text(img)
    cfg = f"--oem 3 --psm {psm} -l {lang}"
    txt = pytesseract.image_to_string(proc, config=cfg) or ""
    return txt

def deaccent(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def upclean(s: str) -> str:
    return re.sub(r"[^\w\s\-\(\)\/\.]", " ", deaccent(s or "").upper())

def normalize_date_guess(datestr: str | None):
    """
    Acepta DD[./ -]MM[./ -]YYYY o DD[./ -]MM[./ -]YY (logica España: DD/MM/AAAA)
    """
    if not datestr: return None
    m = re.search(r'(\d{1,2})[.\-\/ ](\d{1,2})[.\-\/ ](\d{2,4})', datestr)
    if not m: return None
    dd = int(m.group(1)); mm = int(m.group(2)); yy = int(m.group(3))
    if yy < 100:
        # suponer siglo: si yy <= año actual % 100 → 2000+yy; si no → 1900+yy
        nowyy = date.today().year % 100
        yy = 2000 + yy if yy <= nowyy else 1900 + yy
    try:
        return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except Exception:
        return None

def smart_pick(*vals):
    for v in vals:
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return None

# ---------- MRZ (igual que antes, por si lo usas) ----------

def sanitize_line(s: str) -> str:
    return re.sub(r"[^A-Z0-9<]", "", (s or "").upper())

def normalize_td1_line(line: str) -> str:
    s = sanitize_line(line)
    if len(s) > 30: s = s[:30]
    if len(s) < 30: s = s + "<" * (30 - len(s))
    return s

def ocr_bottom_mrz_lines_fast(img: Image.Image):
    w, h = img.size
    crop = img.crop((int(w*0.03), int(h*0.60), int(w*0.97), int(h*0.98)))
    if crop.width > 1200:
        scale = 1200 / crop.width
        crop = crop.resize((int(crop.width*scale), int(crop.height*scale)))
    crop = enhance_text(crop)
    cfg = "--oem 3 --psm 6 -l ocrb -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
    txt = pytesseract.image_to_string(crop, config=cfg) or ""
    lines = [sanitize_line(l) for l in txt.splitlines() if l.strip()]
    if len(lines) > 3: lines = lines[-3:]
    lines = [normalize_td1_line(l) for l in lines]
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

        if fast:
            lines, ocr_raw = ocr_bottom_mrz_lines_fast(img)
            raw = "\n".join(lines)
            optional_td1 = ""
            if lines:
                l1 = lines[0]
                optional_td1 = l1[15:30].replace("<","")
            return jsonify({
                "ok": True,
                "type": "TD1",
                "doc_code": "I",
                "issuing_country": "",
                "numero": "",
                "nacionalidad": "",
                "apellidos": "",
                "nombres": "",
                "sexo": "",
                "nacimiento": None,
                "expiracion": None,
                "optional": "",
                "raw": "",
                "raw_ocr": ocr_raw,
                "mrz_lines": lines,
                "optional_td1": optional_td1,
            })

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
            "nacimiento": normalize_date_guess(d.get("date_of_birth")),
            "expiracion": normalize_date_guess(d.get("expiration_date")),
            "optional": (d.get("personal_number") or d.get("optional_data") or ""),
            "raw": raw_pe,
            "raw_ocr": ocr_raw or raw,
            "mrz_lines": lines,
            "optional_td1": optional_td1,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500

# ---------- ID OCR anverso + reverso ----------

# Heurísticas de extracción
CALLE_WORDS = r"(CALLE|CL|C\/|AVENIDA|AVDA|AVD|PLAZA|PZA|CAMINO|CMNO|CARRETERA|CTRA|PGNO|POLIGONO|URBANIZACION|URB|BARRIO|BJDA|RONDA)"
LOC_WORDS   = r"(LOCALIDAD|MUNICIPIO|POBLACION|POBLACIÓN|CIUDAD|VILLA)"
PROV_WORDS  = r"(PROVINCIA|PROV\.)"
VALIDEZ_WORDS = r"(VALIDEZ|VALIDO HASTA|V[AÁ]LIDO HASTA|CADUCIDAD|EXPIRA|HASTA)"
EXP_WORDS     = r"(EXPEDICION|EXPEDICI[OÓ]N|F\.? EXP\.?|EXP\.)"
PAIS_WORDS    = r"(PAIS|PA[IÍ]S|NACIONALIDAD)"

CP_RE   = r"\b(\d{5})\b"
DATE_RE = r"(\d{1,2}[.\-\/ ]\d{1,2}[.\-\/ ]\d{2,4})"

def extract_dates(txt_front: str, txt_back: str):
    uf = upclean(txt_front); ub = upclean(txt_back)
    # Validez
    validez = None
    for blob in [uf, ub]:
        m1 = re.search(VALIDEZ_WORDS + r".{0,20}" + DATE_RE, blob)
        m2 = re.search(DATE_RE + r".{0,20}" + VALIDEZ_WORDS, blob)
        if m1 and not validez: validez = normalize_date_guess(m1.group(0))
        if m2 and not validez: validez = normalize_date_guess(m2.group(0))
    # Expedición
    exped = None
    for blob in [uf, ub]:
        m1 = re.search(EXP_WORDS + r".{0,20}" + DATE_RE, blob)
        m2 = re.search(DATE_RE + r".{0,20}" + EXP_WORDS, blob)
        if m1 and not exped: exped = normalize_date_guess(m1.group(0))
        if m2 and not exped: exped = normalize_date_guess(m2.group(0))
    return exped, validez

def extract_country(txt_front: str, txt_back: str):
    for blob in [txt_back, txt_front]:
        u = upclean(blob)
        m = re.search(PAIS_WORDS + r".{0,20}([A-Z][A-Z \-]{2,})", u)
        if m:
            cand = m.group(2).strip()
            # Limpiar posibles arrastres
            cand = re.sub(r"[^A-Z \-]", "", cand).strip()
            if cand: return cand
    return "ESPANA"  # por defecto en DNIs españoles

def extract_address(txt_back: str):
    """
    Intenta:
    1) Buscar línea con DOMICILIO y leer 1-2 líneas debajo como dirección.
    2) Buscar patrón de CP + localidad (+ provincia entre paréntesis).
    3) Buscar línea que empiece con palabra de vía (CALLE, AVDA, etc.).
    """
    lines = [l for l in upclean(txt_back).splitlines() if l.strip()]
    domicilio = None
    cp = None
    localidad = None
    provincia = None

    # 1) DOMICILIO
    idx = next((i for i,l in enumerate(lines) if "DOMICILIO" in l or "DOM." in l), None)
    if idx is not None:
        # siguiente(s) líneas como dirección
        cand = " ".join(lines[idx+1: idx+3]).strip()
        if cand:
            domicilio = cand

    # 2) CP + Localidad (+ Provincia)
    joined = "\n".join(lines)
    mcp = re.search(CP_RE + r"\s+([A-ZÑÁÉÍÓÚÜ ]{2,})(?:\s*\(([^)]+)\))?", joined)
    if mcp:
        cp = mcp.group(1)
        localidad = mcp.group(2).strip() if mcp.group(2) else None
        provincia = mcp.group(3).strip() if len(mcp.groups())>=3 else None

    # 3) Línea de vía
    if not domicilio:
        for l in lines:
            if re.search(r"^" + CALLE_WORDS + r"\b", l):
                domicilio = l.strip()
                break

    # Afinar: si domicilio está en mayúsculas "comidas" raras, poda repeticiones
    if domicilio:
        domicilio = re.sub(r"\s{2,}", " ", domicilio)
        # Evita que incluya CP/LOCALIDAD si ya lo hemos separado
        if cp:
            domicilio = re.sub(CP_RE + r".+$", "", domicilio).strip()

    return domicilio, cp, localidad, provincia

@app.post("/idocr")
@require_api_key
def idocr():
    try:
        f_front = request.files.get("front")
        f_back  = request.files.get("back")
        if not f_front or not f_back:
            return jsonify({"ok": False, "error": 'Faltan archivos: front y back'}), 400

        imgf = fix_orientation(f_front.read())
        imgb = fix_orientation(f_back.read())

        # OCR rápido de ambas caras
        txt_front = ocr_image(imgf, lang="spa+eng", psm=6)
        txt_back  = ocr_image(imgb, lang="spa+eng", psm=6)

        # Extracciones
        fecha_exp, fecha_val = extract_dates(txt_front, txt_back)
        pais_res = extract_country(txt_front, txt_back)
        domicilio, cp, loc, prov = extract_address(txt_back)

        return jsonify({
            "ok": True,
            "front_text": txt_front,
            "back_text": txt_back,
            "domicilio": domicilio,
            "cp": cp,
            "localidad": loc,
            "provincia": prov,
            "pais_residencia": pais_res,
            "fecha_expedicion": fecha_exp,
            "fecha_validez": fecha_val,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500
