from flask import Flask, request, jsonify
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ImageOps, ImageFilter, ExifTags
from datetime import date
import io, os, re, traceback
import pytesseract

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Clave API (usará la variable de entorno si existe; si no, este valor por defecto)
API_KEY = os.environ.get("MRZ_API_KEY", "pirulico22")

# Config OCR para MRZ (respaldo)
TESS_MRZ_CONFIG = (
    "--oem 3 --psm 6 -l eng "
    "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789< "
    "-c preserve_interword_spaces=1"
)

# ----------------- Utilidades -----------------

def _ok_auth(req) -> bool:
    key = req.headers.get("X-API-Key", "")
    return (API_KEY == "" or key == API_KEY)

def fix_orientation_open(bytes_jpg: bytes) -> Image.Image:
    """Abrir imagen y corregir orientación EXIF si la hubiera."""
    img = Image.open(io.BytesIO(bytes_jpg))
    try:
        orientation_tag = next((k for k, v in ExifTags.TAGS.items() if v == "Orientation"), None)
        exif = img._getexif() if hasattr(img, "_getexif") else None
        if exif and orientation_tag in exif:
            o = exif[orientation_tag]
            if o == 3:   img = img.rotate(180, expand=True)
            elif o == 6: img = img.rotate(270, expand=True)
            elif o == 8: img = img.rotate(90, expand=True)
    except Exception:
        pass
    return img

def to_jpeg_bytes(img: Image.Image, q=92) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=q)
    out.seek(0)
    return out.getvalue()

def enhance_for_mrz(img: Image.Image) -> Image.Image:
    """Mejora ligera para MRZ: grises + autocontraste + sharpen."""
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.SHARPEN)
    return g

def normalize_date_mrz(yyMMdd: str | None):
    if not yyMMdd:
        return None
    s = re.sub(r"[^0-9]", "", yyMMdd)
    if len(s) != 6:
        return None
    yy = int(s[:2])
    mm = s[2:4]
    dd = s[4:6]
    nowyy = date.today().year % 100
    century = 2000 if yy <= nowyy else 1900
    try:
        return f"{century + yy}-{mm}-{dd}"
    except Exception:
        return None

def ocr_mrz_lines(img: Image.Image):
    """OCR de respaldo: devuelve (lines_norm, text_raw)."""
    text = pytesseract.image_to_string(img, config=TESS_MRZ_CONFIG) or ""
    # Normalizamos: solo A-Z 0-9 y < ; quitamos espacios y rarezas
    lines = []
    for l in text.splitlines():
        s = re.sub(r"[^A-Z0-9<]", "", l.upper())
        s = re.sub(r"<{3,}", lambda m: "<"*min(len(m.group(0)), 15), s)  # limita repetidos
        if s.strip():
            lines.append(s.strip())
    # nos quedamos con las últimas 3 (suele estar abajo)
    if len(lines) > 3:
        lines = lines[-3:]
    return lines, text

def parse_td3(lines):
    """Parser simple TD3 (2 líneas ~44 chars). Devuelve dict o None."""
    if len(lines) < 2:
        return None
    L1 = lines[0]
    L2 = lines[1]
    if len(L1) < 40 or len(L2) < 40:
        return None
    # L1: P<ISS<<SURNAME<<GIVEN<NAMES
    issuing = L1[2:5]
    # Nombres/apellidos: a partir de pos 5 hasta el final, separados por '<<'
    namefield = L1[5:]
    parts = namefield.split("<<", 1)
    surname = (parts[0] or "").replace("<", " ").strip()
    given = (parts[1] if len(parts) > 1 else "").replace("<", " ").strip()

    # L2:
    # number[0:9], check[9], nationality[10:13], birth[13:19], chk[19],
    # sex[20], expiry[21:27], chk[27], optional[28:42], chk[43]
    number = L2[0:9].replace("<", "")
    nationality = L2[10:13]
    birth = L2[13:19]
    sex = L2[20:21]
    expiry = L2[21:27]
    optional = L2[28:42]

    return {
        "mrz_type": "TD3",
        "country": issuing,
        "number": number or None,
        "nationality": nationality or None,
        "date_of_birth": birth or None,
        "sex": sex or None,
        "expiration_date": expiry or None,
        "surname": surname or None,
        "names": given or None,
        "personal_number": optional or None,
        "mrz_text": "\n".join(lines)
    }

def parse_td1(lines):
    """Parser simple TD1 (3 líneas de ~30). Devuelve dict o None."""
    if len(lines) < 3:
        return None
    L1, L2, L3 = lines[0], lines[1], lines[2]
    if len(L1) < 25 or len(L2) < 25 or len(L3) < 25:
        return None
    # TD1 estructura típica:
    # L1: IDxISSCountry + doc number + filler + optional
    # L2: dob[0:6] + chk + sex + expiry[8:14] + chk + nationality[15:18] + optional + chk
    # L3: surname<<given<<...
    # Nota: muchos países varían; hacemos una extracción tolerante
    number = re.sub(r"<", "", L1[5:14])
    country = L1[2:5]
    # L2
    birth = L2[0:6]
    sex = L2[7:8]
    expiry = L2[8:14]
    nationality = L2[15:18]
    # L3
    parts = L3.split("<<", 1)
    surname = (parts[0] or "").replace("<", " ").strip()
    given = (parts[1] if len(parts) > 1 else "").replace("<", " ").strip()

    return {
        "mrz_type": "TD1",
        "country": country or None,
        "number": number or None,
        "nationality": nationality or None,
        "date_of_birth": birth or None,
        "sex": sex or None,
        "expiration_date": expiry or None,
        "surname": surname or None,
        "names": given or None,
        "personal_number": "",
        "mrz_text": "\n".join(lines)
    }

def parse_mrz_fallback(img: Image.Image):
    """OCR MRZ y parser propio TD3/TD1. Devuelve dict o None y debug."""
    enhanced = enhance_for_mrz(img)
    lines, raw = ocr_mrz_lines(enhanced)
    parsed = None
    # Intenta TD3 si parece pasaporte (empieza por P< o líneas largas)
    if any(l.startswith("P<") for l in lines) or any(len(l) >= 40 for l in lines):
        parsed = parse_td3(lines)
    if not parsed and len(lines) >= 3:
        parsed = parse_td1(lines)
    if not parsed and len(lines) >= 2:
        # último intento TD3 tolerante
        parsed = parse_td3(lines)
    return parsed, {"ocr_lines": lines, "ocr_text": raw}

# ----------------- Endpoints -----------------

@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "service": "mrz+idocr",
        "tip": "POST /mrz (multipart image) with optional ?fast=0/1&doc_type=PASSPORT|ID"
    })

@app.post("/mrz")
def mrz():
    if not _ok_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        f = request.files.get("image")
        if not f:
            return jsonify({"ok": False, "error": 'No file field "image" found'}), 400

        raw_bytes = f.read()
        img = fix_orientation_open(raw_bytes)
        w, h = img.size

        fast = request.args.get("fast", "0") == "1"
        doc_type = (request.args.get("doc_type") or request.headers.get("X-Doc-Type") or "").upper().strip()

        debug = {
            "received_doc_type": doc_type or None,
            "received_bytes": len(raw_bytes),
            "image_size": {"w": w, "h": h},
            "strategy": None,
            "passporteye": None,
            "fallback": None
        }

        # FAST: solo OCR ligero para verificar líneas (no parse completo)
        if fast:
            enhanced = enhance_for_mrz(img)
            lines, raw = ocr_mrz_lines(enhanced)
            guess = "TD3" if (doc_type == "PASSPORT" or any(l.startswith("P<") for l in lines)) else ("TD1" if len(lines) >= 3 else "")
            debug["strategy"] = "fast-ocr"
            debug["fallback"] = {"ocr_lines": lines, "ocr_text_len": len(raw)}
            return jsonify({
                "ok": True,
                "received_doc_type": doc_type or None,
                "type": "TD3" if doc_type == "PASSPORT" else guess,
                "mrz_lines": lines,
                "raw_ocr_len": len(raw),
                "debug": debug
            })

        # 1) PassportEye directo
        mrz_obj = read_mrz(io.BytesIO(raw_bytes), save_roi=False)
        if mrz_obj is None:
            # 2) Mejorado
            enhanced = enhance_for_mrz(img)
            mrz_obj = read_mrz(io.BytesIO(to_jpeg_bytes(enhanced)), save_roi=False)
            debug["strategy"] = "enhanced-passporteye"
        else:
            debug["strategy"] = "passporteye"

        if mrz_obj is None:
            # 3) Rotado 180
            rot = img.rotate(180, expand=True)
            mrz_obj = read_mrz(io.BytesIO(to_jpeg_bytes(rot)), save_roi=False)
            if debug["strategy"] is None:
                debug["strategy"] = "rot180-passporteye"

        data = None
        if mrz_obj is not None:
            d = mrz_obj.to_dict()
            debug["passporteye"] = {
                "mrz_type": d.get("mrz_type"),
                "has_text": bool(d.get("mrz_text")),
                "country": d.get("country"),
            }
            data = {
                "mrz_type": d.get("mrz_type") or ("TD3" if doc_type == "PASSPORT" else None),
                "country": d.get("country"),
                "number": d.get("number"),
                "nationality": (d.get("nationality") or "").upper() if d.get("nationality") else None,
                "date_of_birth": d.get("date_of_birth"),
                "sex": (d.get("sex") or "").upper() if d.get("sex") else None,
                "expiration_date": d.get("expiration_date"),
                "surname": d.get("surname"),
                "names": d.get("names"),
                "personal_number": d.get("personal_number") or d.get("optional_data") or "",
                "mrz_text": d.get("mrz_text") or ""
            }

        # 4) Fallback OCR propio si PassportEye falló o no dio campos
        need_fallback = (data is None) or (not data.get("number"))
        if need_fallback:
            parsed, fb = parse_mrz_fallback(img)
            debug["fallback"] = fb
            if parsed:
                # si nos pidieron PASSPORT, fuerza TD3
                if doc_type == "PASSPORT":
                    parsed["mrz_type"] = "TD3"
                data = parsed
                if debug["strategy"] is None:
                    debug["strategy"] = "fallback-ocr-parse"

        if data is None:
            return jsonify({
                "ok": False,
                "error": "No se pudo leer MRZ",
                "debug": debug
            }), 422

        # Normalización final para el cliente
        mrz_type = data.get("mrz_type") or ("TD3" if doc_type == "PASSPORT" else None)
        nacimiento = normalize_date_mrz(data.get("date_of_birth"))
        expiracion = normalize_date_mrz(data.get("expiration_date"))

        # Si el backend sabe que es pasaporte, reflejamos TD3 siempre
        if doc_type == "PASSPORT":
            mrz_type = "TD3"

        resp = {
            "ok": True,
            "received_doc_type": doc_type or None,
            "type": mrz_type,
            "doc_code": None,  # (PassportEye devuelve en d['type'] pero no es vital)
            "issuing_country": data.get("country"),
            "numero": data.get("number"),
            "nacionalidad": data.get("nationality"),
            "apellidos": data.get("surname"),
            "nombres": data.get("names"),
            "sexo": data.get("sex"),
            "nacimiento": nacimiento,
            "expiracion": expiracion,
            "optional": data.get("personal_number") or "",
            "raw": data.get("mrz_text") or "",
            "debug": debug
        }
        return jsonify(resp)

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"EXCEPTION: {str(e)}",
            "trace": traceback.format_exc()[:2000]
        }), 500

# Placeholder para futuro OCR de ID (mantiene compatibilidad si lo llamas)
@app.post("/idocr")
def id_ocr():
    if not _ok_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    return jsonify({"ok": False, "error": "ID OCR no implementado todavía"}), 501

if __name__ == "__main__":
    # Para pruebas locales: gunicorn en Render se encarga en producción
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
