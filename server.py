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

LANGS_ID = "eng+spa+fra+deu+ita+por+nld"  # multi-país UE
TESS_MRZ_CONFIG = "--oem 3 --psm 6 -l eng -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"

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

@app.route("/mrz", methods=["OPTIONS"])
@app.route("/idocr", methods=["OPTIONS"])
def options_any():
    return make_response(("", 204))

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

def to_jpeg_bytes(img: Image.Image, q=92) -> bytes:
    out = io.BytesIO(); img.save(out, format="JPEG", quality=q); out.seek(0); return out.getvalue()

def ocr_image(img: Image.Image, lang=LANGS_ID, psm=6) -> str:
    proc = enhance_text(img)
    cfg = f"--oem 3 --psm {psm} -l {lang}"
    return pytesseract.image_to_string(proc, config=cfg) or ""

def deaccent(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def upclean(s: str) -> str:
    return re.sub(r"[^\w\s\-\(\)\/\.]", " ", deaccent(s or "").upper())

def normalize_date_guess(datestr: str | None):
    if not datestr: return None
    m = re.search(r'(\d{1,2})[.\-\/ ](\d{1,2})[.\-\/ ](\d{2,4})', datestr)
    if not m: return None
    dd = int(m.group(1)); mm = int(m.group(2)); yy = int(m.group(3))
    if yy < 100:
        nowyy = date.today().year % 100
        yy = 2000 + yy if yy <= nowyy else 1900 + yy
    try:
        return f"{yy:04d}-{mm:02d}-{dd:02d}"
    except Exception:
        return None

def normalize_date_mrz(yyMMdd: str | None):
    if not yyMMdd or len(yyMMdd) != 6: return None
    yy = int(yyMMdd[:2]); mm = yyMMdd[2:4]; dd = yyMMdd[4:6]
    nowyy = date.today().year % 100
    century = 2000 if yy <= nowyy else 1900
    return f"{century + yy}-{mm}-{dd}"

# ---------- MRZ helpers ----------
def sanitize_line(s: str) -> str:
    return re.sub(r"[^A-Z0-9<]", "", (s or "").upper())

def normalize_td1_line(line: str) -> str:
    s = sanitize_line(line)
    s = s[:30] if len(s) > 30 else s.ljust(30, '<')
    return s

def ocr_bottom_mrz_lines_fast(img: Image.Image):
    w, h = img.size
    crop = img.crop((int(w*0.03), int(h*0.60), int(w*0.97), int(h*0.98)))
    if crop.width > 1200:
        scale = 1200 / crop.width
        crop = crop.resize((int(crop.width*scale), int(crop.height*scale)))
    crop = enhance_text(crop)
    txt = pytesseract.image_to_string(crop, config=TESS_MRZ_CONFIG) or ""
    lines = [sanitize_line(l) for l in txt.splitlines() if l.strip()]
    if len(lines) > 3: lines = lines[-3:]
    lines = [normalize_td1_line(l) for l in lines]
    return lines, txt

def try_read_mrz(bytes_jpg: bytes, psm_list=(6,7,11)):
    params = [f"--oem 3 --psm {p} -l eng -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<" for p in psm_list]
    for p in params:
        mrz = read_mrz(io.BytesIO(bytes_jpg), save_roi=True, extra_cmdline_params=p)
        if mrz is not None: return mrz
    # rotado 180
    img = Image.open(io.BytesIO(bytes_jpg))
    rot = img.rotate(180, expand=True)
    rb = to_jpeg_bytes(rot, 92)
    for p in params:
        mrz = read_mrz(io.BytesIO(rb), save_roi=True, extra_cmdline_params=p)
        if mrz is not None: return mrz
    return None

# ---------- Dirección multi-idioma (best effort) ----------
ADDRESS_WORDS = r"(CALLE|CL|C\/|AVENIDA|AVDA|AVD|PLAZA|PZA|CAMINO|CMNO|CARRETERA|CTRA|CRTA|PGNO|POLIGONO|URBANIZACION|URB|BARRIO|RONDA|PASAJE|PJE|TRAVESIA|TRV|RUE|AVENUE|BOULEVARD|BD|CHEMIN|PLACE|IMPASSE|ALLEE|STRASSE|STR\.|PLATZ|WEG|ALLEE|VIA|VICOLO|PIAZZA|CORSO|LARGO|RUA|AV\.|AVENIDA|ESTRADA|TRAVESSA|STRAAT|LAAN|PLEIN)"
CP_ANY = r"\b([A-Z\-]{0,2}\d{3,6}[A-Z]?)\b"  # muy laxo para códigos postales varios

def likely_address_line(u: str) -> bool:
    if any(k in u for k in ["NACIONALIDAD","NOMBRE","APELLIDOS","DOCUMENTO","IDENTITY","CARD","NUM SOPORTE","SOPORTE","PLACE OF BIRTH","LUGAR","NAISSANCE","GEBURTSORT","NASCITA","NASCIMENTO","MRZ"]):
        return False
    if re.search(ADDRESS_WORDS, u): return True
    if re.search(r"\d", u): return True
    return (8 <= len(u) <= 52) and u == u.upper()

def extract_address_generic(back_text: str):
    lines = [l for l in upclean(back_text).splitlines() if l.strip()]
    domicilio = None; cp=None; loc=None; prov=None
    # primeras 1-2 líneas que parezcan dirección
    addr=[]
    for l in lines:
        if likely_address_line(l):
            addr.append(l)
            if len(addr)>=2: break
        elif addr: break
    if addr:
        domicilio = re.sub(r"\s{2,}", " ", " ".join(addr)).strip()
    # algún CP
    joined="\n".join(lines)
    m=re.search(CP_ANY, joined)
    if m: cp=m.group(1)
    return domicilio, cp, loc, prov

# ---------- Endpoints ----------
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
        txt_front = ocr_image(imgf, lang=LANGS_ID, psm=6)
        txt_back  = ocr_image(imgb, lang=LANGS_ID, psm=6)

        # Fechas (busca en cualquier idioma, DD/MM/AAAA etc.)
        f_dates = [d for d in re.findall(r'(\d{1,2}[.\-\/ ]\d{1,2}[.\-\/ ]\d{2,4})', upclean(txt_front)) ]
        f_dates = [normalize_date_guess(x) for x in f_dates if x]
        fecha_exp = f_dates[0] if len(f_dates)>=1 else None
        fecha_val = f_dates[-1] if len(f_dates)>=2 else None

        domicilio, cp, loc, prov = extract_address_generic(txt_back)

        # País residencia: intenta código ISO-3 de NACIONALIDAD/NATIONALITY
        uf = upclean(txt_front)
        nat = None
        m = re.search(r"(NACIONALIDAD|NATIONALITY|NATIONALITE|STAATSANGEHORIGKEIT|CITTADINANZA)[^A-Z0-9]{0,10}([A-Z]{3})", uf)
        if m: nat = m.group(2)
        pais = "ESPAÑA" if nat=="ESP" else (nat or None)

        return jsonify({
            "ok": True,
            "front_text": txt_front,
            "back_text": txt_back,
            "domicilio": domicilio,
            "cp": cp,
            "localidad": loc,
            "provincia": prov,
            "pais_residencia": pais,
            "fecha_expedicion": fecha_exp,
            "fecha_validez": fecha_val,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500

@app.post("/mrz")
@require_api_key
def mrz():
    try:
        f = request.files.get("image")
        if not f: return jsonify({"ok": False, "error": 'No file "image"'}), 400
        img = fix_orientation(f.read())
        fast = (request.args.get("fast", "0") == "1")
        if fast:
            lines, ocr_raw = ocr_bottom_mrz_lines_fast(img)
            raw = "\n".join(lines)
            optional_td1 = ""
            if lines:
                l1 = lines[0]
                optional_td1 = l1[15:30].replace("<","")
            return jsonify({
                "ok": True, "type": "TD1", "doc_code": "I", "issuing_country": "", "numero": "",
                "nacionalidad": "", "apellidos": "", "nombres": "", "sexo": "", "nacimiento": None, "expiracion": None,
                "optional": "", "raw": "", "raw_ocr": ocr_raw, "mrz_lines": lines, "optional_td1": optional_td1
            })
        mrz_obj = try_read_mrz(to_jpeg_bytes(img, 92), psm_list=(6,7,11))
        d = mrz_obj.to_dict() if mrz_obj else {}
        raw_pe = d.get("mrz_text") or ""
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
            "nacimiento": normalize_date_mrz(d.get("date_of_birth")) if d.get("date_of_birth") else None,
            "expiracion": normalize_date_mrz(d.get("expiration_date")) if d.get("expiration_date") else None,
            "optional": (d.get("personal_number") or d.get("optional_data") or ""),
            "raw": raw_pe,
            "raw_ocr": raw_pe,
            "mrz_lines": [],
            "optional_td1": ""
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500
