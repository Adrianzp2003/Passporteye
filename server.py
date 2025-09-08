from flask import Flask, request, jsonify
from flask_cors import CORS
from passporteye import read_mrz
from PIL import Image, ImageOps, ImageFilter, ExifTags
from datetime import date
import io, os, re, traceback

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Tu API key (usa env si existe)
API_KEY = os.environ.get("MRZ_API_KEY", "pirulico22")

def ok_auth(req):
    key = req.headers.get("X-API-Key", "")
    return (API_KEY == "" or key == API_KEY)

def fix_orientation_open(raw_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw_bytes))
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

def enhance(img: Image.Image) -> Image.Image:
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.SHARPEN)
    return g

def bottom_crop(img: Image.Image, top_ratio=0.55, bottom_ratio=0.98) -> Image.Image:
    w,h = img.size
    top = int(h*top_ratio); bot = int(h*bottom_ratio)
    left = int(w*0.03); right = int(w*0.97)
    return img.crop((left, top, right, bot))

def normalize_date(yyMMdd: str | None):
    if not yyMMdd: return None
    s = re.sub(r"[^0-9]", "", yyMMdd)
    if len(s) != 6: return None
    yy = int(s[:2]); mm = s[2:4]; dd = s[4:6]
    nowyy = date.today().year % 100
    century = 2000 if yy <= nowyy else 1900
    return f"{century + yy}-{mm}-{dd}"

@app.get("/")
def index():
    return jsonify({"ok": True, "service":"mrz", "tip":"POST /mrz (multipart image), ?doc_type=PASSPORT"}), 200

@app.post("/mrz")
def mrz():
    if not ok_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        f = request.files.get("image")
        if not f:
            return jsonify({"ok": False, "error": 'No file field "image"'}), 400

        raw = f.read()
        img0 = fix_orientation_open(raw)
        w,h = img0.size
        doc_type = (request.args.get("doc_type") or request.headers.get("X-Doc-Type") or "").upper().strip()

        # Estrategia simple y robusta con PassportEye
        tried = []

        # A) Imagen completa
        mrz = read_mrz(io.BytesIO(to_jpeg_bytes(img0)), save_roi=False)
        tried.append("full")
        if mrz is None:
            # B) Mejorada
            mrz = read_mrz(io.BytesIO(to_jpeg_bytes(enhance(img0))), save_roi=False)
            tried.append("enhanced")

        if mrz is None:
            # C) Recorte inferior (banda MRZ)
            crop = bottom_crop(img0, 0.55, 0.98)
            mrz = read_mrz(io.BytesIO(to_jpeg_bytes(enhance(crop))), save_roi=False)
            tried.append("bottom_crop+enhance")

        if mrz is None:
            # D) Rotado 180 (por si está al revés)
            rot = img0.rotate(180, expand=True)
            mrz = read_mrz(io.BytesIO(to_jpeg_bytes(enhance(rot))), save_roi=False)
            tried.append("rotate180+enhance")

        if mrz is None:
            return jsonify({
                "ok": False,
                "error": "No se pudo leer MRZ",
                "debug": {"received_bytes": len(raw), "image_size": {"w":w,"h":h}, "doc_type": doc_type or None, "tried": tried}
            }), 422

        d = mrz.to_dict()  # campos de PassportEye
        resp = {
            "ok": True,
            "received_doc_type": doc_type or None,
            "type": "TD3" if doc_type == "PASSPORT" else d.get("mrz_type"),
            "doc_code": d.get("type"),
            "issuing_country": d.get("country"),
            "numero": d.get("number"),
            "nacionalidad": (d.get("nationality") or "").upper() if d.get("nationality") else None,
            "apellidos": d.get("surname"),
            "nombres": d.get("names"),
            "sexo": (d.get("sex") or "").upper() if d.get("sex") else None,
            "nacimiento": normalize_date(d.get("date_of_birth")) if d.get("date_of_birth") else None,
            "expiracion": normalize_date(d.get("expiration_date")) if d.get("expiration_date") else None,
            "optional": d.get("personal_number") or d.get("optional_data") or "",
            "raw": d.get("mrz_text") or "",
            "debug": {"tried": tried, "image_size": {"w":w,"h":h}}
        }
        return jsonify(resp)

    except Exception as e:
        return jsonify({"ok": False, "error": f"EXCEPTION: {str(e)}", "trace": traceback.format_exc()[:1500]}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
