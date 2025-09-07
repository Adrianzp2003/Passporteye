from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ExifTags, ImageOps, ImageFilter
from datetime import date
from functools import wraps
import io, os, traceback, subprocess, pytesseract, re, unicodedata

app = Flask(__name__)
CORS(app, resources={ r"/": {"origins": "*"}, r"/mrz": {"origins": "*"}, r"/idocr": {"origins": "*"}})

API_KEY = os.environ.get("MRZ_API_KEY", "pirulico22")

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
    out = io.BytesIO(); img.save(out, format="JPEG", quality=quality); out.seek(0); return out.getvalue()

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

# ---------- MRZ util ----------
def sanitize_line(s: str) -> str:
    return re.sub(r"[^A-Z0-9<]", "", (s or "").upper())

def normalize_td1_line(line: str) -> str:
    s = sanitize_line(line)
    if len(s) > 30: s = s[:30]
    if len(s) < 30: s = s + "<" * (30 - s.length)
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
    lines = [l[:30].ljust(30, '<') for l in lines]
    return lines, txt

def try_read_mrz(bytes_jpg: bytes, psm_list=(6,7,11)):
    params = [f"--oem 3 --psm {p} -l ocrb" for p in psm_list]
    for p in params:
        mrz = read_mrz(io.BytesIO(bytes_jpg), save_roi=True, extra_cmdline_params=p)
        if mrz is not None:
            return mrz
    img = Image.open(io.BytesIO(bytes_jpg))
    rot = img.rotate(180, expand=True)
    rb = to_jpeg_bytes(rot, 92)
    for p in params:
        mrz = read_mrz(io.BytesIO(rb), save_roi=True, extra_cmdline_params=p)
        if mrz is not None:
            return mrz
    return None

# ---------- Provincias (para detectar residencia/provincia) ----------
PROVINCIAS = {
"ALAVA","ALBACETE","ALICANTE","ALMERIA","ASTURIAS","AVILA","BADAJOZ","BARCELONA","BURGOS","CACERES","CADIZ","CANTABRIA",
"CASTELLON","CEUTA","CIUDAD REAL","CORDOBA","CUENCA","GIRONA","GRANADA","GUADALAJARA","GUIPUZCOA","HUELVA","HUESCA",
"ILLES BALEARS","JAEN","LA CORUNA","A CORUNA","LA RIOJA","LAS PALMAS","LEON","LLEIDA","LUGO","MADRID","MALAGA","MELILLA",
"MURCIA","NAVARRA","OURENSE","PALENCIA","PONTEVEDRA","SALAMANCA","SEGOVIA","SEVILLA","SORIA","TARRAGONA","SANTA CRUZ DE TENERIFE",
"TERUEL","TOLEDO","VALENCIA","VALLADOLID","VIZCAYA","BIZKAIA","ZAMORA","ZARAGOZA","ALAVA","ARABA"
}

# ---------- Extracciones específicas DNI ----------
LABEL_CUTS = ("LUGAR DE NACIMIENTO","LUGAR  DE  NACIMIENTO","LUGAR DE  NACIMIENTO","LUGAR  DE NACIMIENTO","LUGAR NACIMIENTO")

def split_residence_section(back_text: str):
    lines = [l for l in upclean(back_text).splitlines() if l.strip()]
    idx = None
    for i,l in enumerate(lines):
        if any(tag in l for tag in LABEL_CUTS):
            idx = i
            break
    res_lines = lines if idx is None else lines[:idx]
    return res_lines

ADDRESS_WORDS = r"(CALLE|CL|C\/|AVENIDA|AVDA|AVD|PLAZA|PZA|CAMINO|CMNO|CARRETERA|CTRA|CRTA|PGNO|POLIGONO|URBANIZACION|URB|BARRIO|RONDA|PASAJE|PJE|TRAVESIA|TRV)"
CP_RE   = r"\b(\d{5})\b"
DATE_RE = r"(\d{1,2}[.\-\/ ]\d{1,2}[.\-\/ ]\d{2,4})"

def likely_address_line(u: str) -> bool:
    if any(k in u for k in ["NACIONALIDAD","NOMBRE","APELLIDOS","DOCUMENTO","IDENTITY","CARD","NUM SOPORTE","SOPORTE","LUGAR","NACIMIENTO"]):
        return False
    if re.search(ADDRESS_WORDS, u): return True
    if re.search(r"\d", u): return True
    return (8 <= len(u) <= 45) and u == u.upper()

def extract_address_residence(back_text: str):
    res_lines = split_residence_section(back_text)
    domicilio = None; cp=None; loc=None; prov=None

    # 1) Domicilio (primeras 1-2 líneas que parecen dirección)
    addr_lines = []
    for l in res_lines:
        u = l.strip()
        if likely_address_line(u):
            addr_lines.append(u)
            if len(addr_lines) >= 2: break
        elif addr_lines:
            break
    if addr_lines:
        domicilio = " ".join(addr_lines)
        domicilio = re.sub(r"\s{2,}", " ", domicilio).strip()

    # 2) CP
    joined = "\n".join(res_lines)
    mcp = re.search(CP_RE, joined)
    if mcp: cp = mcp.group(1)

    # 3) Provincia y localidad (sólo en sección de residencia)
    upp = [l for l in res_lines if l == l.upper() and '<' not in l]
    # limpia líneas demasiado "vacías"
    upp = [re.sub(r"\s{2,}", " ", l).strip() for l in upp if len(l.strip())>=3 and not re.search(r"\d", l)]
    prov = next((l for l in upp if deaccent(l) in PROVINCIAS), None)
    if prov:
        # localidad: otra línea en mayúsculas distinta de la provincia
        loc = next((l for l in upp if l != prov and deaccent(l) not in PROVINCIAS), None)

    return domicilio, cp, loc, prov

def extract_country(front_text: str, back_text: str, mrz_nat: str | None):
    uf = upclean(front_text); ub = upclean(back_text)
    # Prioriza NACIONALIDAD: ESP / FRA / ...
    m = re.search(r"NACIONALIDAD[^A-Z0-9]{0,10}([A-Z]{3})", uf)
    if m:
        code = m.group(1)
        return "ESPAÑA" if code == "ESP" else code
    if mrz_nat:
        return "ESPAÑA" if mrz_nat.upper()=="ESP" else mrz_nat.upper()
    # Último recurso: España
    return "ESPAÑA"

def find_all_dates(text: str):
    text = upclean(text)
    return [normalize_date_guess(x) for x in re.findall(DATE_RE, text)]

def parse_mrz_inline(back_text: str):
    # Busca patrón de MRZ TD1 en texto OCR del reverso: YYMMDD[MF<]YYMMDD
    u = re.sub(r"\s","", upclean(back_text))
    m = re.search(r"(\d{6})[MF<](\d{6})", u)
    if not m: return None, None
    birth = normalize_date_mrz(m.group(1))
    exp   = normalize_date_mrz(m.group(2))
    return birth, exp

def extract_dates(front_text: str, back_text: str):
    # Usa anverso si tiene 2 fechas; si no, completa con MRZ del reverso
    f_dates = [d for d in find_all_dates(front_text) if d]
    birth_mrz, exp_mrz = parse_mrz_inline(back_text)
    exped, validez = None, None

    # Heurística: en el anverso suelen ir "EMISIÓN" y "VALIDEZ" la misma línea
    if len(f_dates) >= 2:
        # orden cronológico
        f_dates_sorted = sorted(f_dates)
        exped = f_dates_sorted[0]
        validez = f_dates_sorted[-1]

    if not validez and exp_mrz:
        validez = exp_mrz

    return exped, validez, birth_mrz

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

        # OCR anverso/reverso
        txt_front = ocr_image(imgf, lang="spa+eng", psm=6)
        txt_back  = ocr_image(imgb, lang="spa+eng", psm=6)

        # MRZ inline del reverso (para nacion./caducidad/nacimiento)
        birth_mrz, exp_mrz = parse_mrz_inline(txt_back)

        # Domicilio, CP, Localidad, Provincia (sólo sección residencia)
        domicilio, cp, loc, prov = extract_address_residence(txt_back)

        # Fechas (emisión / validez)
        fecha_exp, fecha_val, birth_from_mrz = extract_dates(txt_front, txt_back)
        if not fecha_val and exp_mrz:
            fecha_val = exp_mrz

        # País de residencia
        pais_res = extract_country(txt_front, txt_back, "ESP")  # DNI español: ESP si no se deduce

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
            "nacimiento_mrz": birth_from_mrz or birth_mrz,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500

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
            l1 = l1[:30].ljust(30,'<')
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
            "nacimiento": normalize_date_mrz(d.get("date_of_birth")) if d.get("date_of_birth") else None,
            "expiracion": normalize_date_mrz(d.get("expiration_date")) if d.get("expiration_date") else None,
            "optional": (d.get("personal_number") or d.get("optional_data") or ""),
            "raw": raw_pe,
            "raw_ocr": ocr_raw or raw,
            "mrz_lines": lines,
            "optional_td1": optional_td1,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:2000]}), 500
