"""
Signatures & Fill-and-Sign.

Two kinds of signing live here:

1. VISUAL signing / Fill & Sign  (/api/pdf/{doc_id}/sign/apply, /sign/stamp)
   Stamps signature/initials PNGs (with alpha), real searchable text (free text,
   dates), and vector check marks / crosses directly into the page content
   stream. Optional "lock" bakes every annotation and form field into page
   content so nothing remains editable.

2. DIGITAL signing (cryptographic, PAdES-style CMS via pyHanko)
   (/api/pdf/signing/certificates, /api/pdf/{doc_id}/sign/digital,
    /api/pdf/{doc_id}/sign/validate, /api/pdf/signing/validate)
   Generates a self-signed X.509 identity on demand (stored under
   UPLOAD_DIR/_certs/<cert_id>/, never inside the repo), signs the document
   incrementally with a visible signature field showing the signature image,
   and validates every embedded signature (signer, time, intact / modified).

COORDINATES: every rect / point accepted by this module is in PDF points with a
TOP-LEFT origin, in the page's *visible* (rotated) frame, i.e. the same frame as
the page image the frontend renders (page.rect in PyMuPDF). Conversion to the
unrotated PyMuPDF frame (page.derotation_matrix) and to bottom-left PDF user
space (page.transformation_matrix) happens here.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import fitz  # PyMuPDF
from fastapi import APIRouter, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel, Field

from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf")

# Same resolution rule as backend/main.py. Tests monkeypatch this attribute.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

_FONTS = {"helv": "helv", "times": "tiro", "courier": "cour"}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _validate_uuid(value: str, what: str = "document ID"):
    if not _UUID_RE.match(value or ""):
        raise HTTPException(status_code=400, detail=f"Invalid {what}")


def _doc_path(doc_id: str) -> Path:
    _validate_uuid(doc_id)
    path = UPLOAD_DIR / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def _certs_dir() -> Path:
    d = UPLOAD_DIR / "_certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decode_png(data: str) -> bytes:
    """Decode base64 (optionally a data: URL) into normalised RGBA PNG bytes."""
    if not data:
        raise HTTPException(status_code=400, detail="Image data is required")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid base64 image data")
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Image is empty or too large")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(status_code=400, detail="Image data is not a readable image")
    img = img.convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _hex_to_rgb(color: Optional[str]) -> tuple[float, float, float]:
    if not color:
        return (0.0, 0.0, 0.0)
    if not _HEX_RE.match(color):
        raise HTTPException(status_code=400, detail=f"Invalid color: {color}")
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _check_rect(page: fitz.Page, rect: list[float]) -> fitz.Rect:
    """Validate a visible-frame rect and return it as fitz.Rect (visible frame)."""
    if len(rect) != 4:
        raise HTTPException(status_code=400, detail="rect must be [x0, y0, x1, y1]")
    r = fitz.Rect(rect).normalize()
    if r.is_empty or r.width < 1 or r.height < 1:
        raise HTTPException(status_code=400, detail="rect is empty")
    visible = page.rect  # rotated frame, top-left origin
    if not visible.intersects(r):
        raise HTTPException(status_code=400, detail="rect lies outside the page")
    return r


def _to_unrotated(page: fitz.Page, r: fitz.Rect) -> fitz.Rect:
    """Visible (rotated) frame -> unrotated PyMuPDF frame."""
    if page.rotation == 0:
        return fitz.Rect(r)
    return (fitz.Rect(r) * page.derotation_matrix).normalize()


def _pt_to_unrotated(page: fitz.Page, p: fitz.Point) -> fitz.Point:
    if page.rotation == 0:
        return fitz.Point(p)
    return fitz.Point(p) * page.derotation_matrix


def _get_page(doc: fitz.Document, page_num: int) -> fitz.Page:
    if page_num < 0 or page_num >= len(doc):
        raise HTTPException(status_code=400, detail="Invalid page number")
    return doc[page_num]


def _save_in_place(doc: fitz.Document, file_path: Path):
    tmp = str(file_path) + ".sign.tmp"
    doc.save(tmp, garbage=1, deflate=True)
    doc.close()
    os.replace(tmp, str(file_path))


def _has_signatures(doc: fitz.Document) -> bool:
    for page in doc:
        for w in page.widgets() or []:
            if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and w.is_signed:
                return True
    return False


def _lock_document(doc: fitz.Document) -> dict:
    """Bake annotations + form fields into page content (they stop being editable)."""
    annots = 0
    widgets = 0
    for page in doc:
        annots += len(list(page.annots() or []))
        widgets += len(list(page.widgets() or []))
    doc.bake(annots=True, widgets=True)
    return {"annotations_flattened": annots, "fields_flattened": widgets}


# ─── Visual signing / Fill & Sign ────────────────────────────────────────────


class SignItem(BaseModel):
    type: Literal["image", "text", "date", "check", "cross", "dot", "line"]
    page: int
    rect: list[float]  # visible frame, PDF points, top-left origin [x0, y0, x1, y1]
    image: Optional[str] = None  # base64 PNG (type=image)
    text: Optional[str] = None  # type=text|date
    font_size: Optional[float] = None  # default: fit rect height
    font: Optional[str] = "helv"  # helv | times | courier
    color: Optional[str] = "#000000"


class ApplyRequest(BaseModel):
    items: list[SignItem] = Field(..., min_length=1, max_length=200)
    lock: bool = False


class StampRequest(BaseModel):
    page: int
    rect: list[float]
    image: str
    lock: bool = False


def _draw_text(page: fitz.Page, r: fitz.Rect, item: SignItem):
    text = (item.text or "").replace("\r", "")
    if item.type == "date" and not text:
        text = datetime.now().strftime("%m/%d/%Y")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text item has no text")
    fontname = _FONTS.get(item.font or "helv")
    if not fontname:
        raise HTTPException(status_code=400, detail=f"Unknown font: {item.font}")
    lines = text.split("\n")
    font = fitz.Font(fontname)
    fs = item.font_size or max(4.0, min(r.height / (1.2 * len(lines)), 72.0))
    if fs <= 0 or fs > 400:
        raise HTTPException(status_code=400, detail="font_size out of range")
    color = _hex_to_rgb(item.color)
    # Baseline of each line in the visible frame, then rotate into page space.
    for i, line in enumerate(lines):
        if not line:
            continue
        baseline_visible = fitz.Point(r.x0, r.y0 + font.ascender * fs + i * fs * 1.2)
        pt = _pt_to_unrotated(page, baseline_visible)
        page.insert_text(pt, line, fontsize=fs, fontname=fontname, color=color,
                         rotate=page.rotation)


def _draw_mark(page: fitz.Page, r: fitz.Rect, item: SignItem):
    color = _hex_to_rgb(item.color)
    w = max(0.8, min(r.width, r.height) * 0.12)

    def P(fx: float, fy: float) -> fitz.Point:  # fractional point in visible rect
        return _pt_to_unrotated(page, fitz.Point(r.x0 + fx * r.width, r.y0 + fy * r.height))

    shape = page.new_shape()
    if item.type == "check":
        shape.draw_polyline([P(0.08, 0.55), P(0.38, 0.85), P(0.92, 0.15)])
        shape.finish(color=color, width=w, closePath=False, lineCap=1, lineJoin=1)
    elif item.type == "cross":
        shape.draw_line(P(0.12, 0.12), P(0.88, 0.88))
        shape.draw_line(P(0.88, 0.12), P(0.12, 0.88))
        shape.finish(color=color, width=w, lineCap=1)
    elif item.type == "dot":
        c = P(0.5, 0.5)
        shape.draw_circle(c, min(r.width, r.height) * 0.3)
        shape.finish(color=color, fill=color, width=0)
    elif item.type == "line":
        shape.draw_line(P(0.0, 0.5), P(1.0, 0.5))
        shape.finish(color=color, width=max(0.6, r.height * 0.15), lineCap=1)
    shape.commit(overlay=True)


def _apply_items(doc: fitz.Document, items: list[SignItem]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        page = _get_page(doc, item.page)
        r = _check_rect(page, item.rect)
        if item.type == "image":
            png = _decode_png(item.image or "")
            page.insert_image(_to_unrotated(page, r), stream=png, keep_proportion=True,
                              overlay=True, rotate=page.rotation)
        elif item.type in ("text", "date"):
            _draw_text(page, r, item)
        else:
            _draw_mark(page, r, item)
        counts[item.type] = counts.get(item.type, 0) + 1
    return counts


@router.post("/{doc_id}/sign/apply")
async def apply_sign_items(doc_id: str, req: ApplyRequest):
    """Burn signatures, initials, text, dates and marks into page content."""
    file_path = _doc_path(doc_id)
    doc = fitz.open(str(file_path))
    try:
        if _has_signatures(doc):
            raise HTTPException(
                status_code=409,
                detail="Document carries a digital signature; editing it would invalidate the signature.",
            )
        # Validate pages up front so we never snapshot a request that will fail.
        for it in req.items:
            _check_rect(_get_page(doc, it.page), it.rect)
        snapshot(doc_id, f"Fill & Sign: {len(req.items)} item(s)")
        counts = _apply_items(doc, req.items)
        lock_info = _lock_document(doc) if req.lock else None
        _save_in_place(doc, file_path)
    except HTTPException:
        if not doc.is_closed:
            doc.close()
        raise
    return {"status": "ok", "applied": counts, "locked": bool(req.lock), "lock": lock_info}


@router.post("/{doc_id}/sign/stamp")
async def stamp_signature(doc_id: str, req: StampRequest):
    """Stamp one transparent PNG signature at rect (convenience wrapper)."""
    return await apply_sign_items(
        doc_id,
        ApplyRequest(items=[SignItem(type="image", page=req.page, rect=req.rect, image=req.image)],
                     lock=req.lock),
    )


@router.post("/{doc_id}/sign/lock")
async def lock_document(doc_id: str):
    """Flatten all annotations and form fields into page content."""
    file_path = _doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if _has_signatures(doc):
        doc.close()
        raise HTTPException(status_code=409, detail="Document carries a digital signature; cannot flatten it.")
    snapshot(doc_id, "Lock document (flatten annotations and fields)")
    info = _lock_document(doc)
    _save_in_place(doc, file_path)
    return {"status": "ok", **info}


# ─── Certificates ────────────────────────────────────────────────────────────


class CertificateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = Field(None, max_length=200)
    organization: Optional[str] = Field(None, max_length=200)
    passphrase: Optional[str] = Field(None, max_length=200)
    valid_years: int = Field(5, ge=1, le=20)


def _cert_info(cert_id: str) -> dict:
    meta = _certs_dir() / cert_id / "meta.json"
    if not meta.exists():
        raise HTTPException(status_code=404, detail="Certificate not found")
    return json.loads(meta.read_text())


@router.post("/signing/certificates")
async def create_certificate(req: CertificateRequest):
    """Generate a self-signed signing identity (RSA-2048, SHA-256)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if req.organization and req.organization.strip():
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, req.organization.strip()))
    if req.email and req.email.strip():
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, req.email.strip()))
    subject = x509.Name(attrs)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365 * req.valid_years))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=True, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION,
                                   x509.ObjectIdentifier("1.3.6.1.4.1.311.10.3.12")]),  # MS document signing
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if req.email and req.email.strip():
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(req.email.strip())]), critical=False
        )
    cert = builder.sign(key, hashes.SHA256())

    enc = (serialization.BestAvailableEncryption(req.passphrase.encode())
           if req.passphrase else serialization.NoEncryption())
    cert_id = str(uuid.uuid4())
    d = _certs_dir() / cert_id
    d.mkdir(parents=True)
    key_path = d / "key.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8, enc))
    os.chmod(key_path, 0o600)
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    info = {
        "cert_id": cert_id,
        "name": name,
        "email": (req.email or "").strip() or None,
        "organization": (req.organization or "").strip() or None,
        "serial": format(cert.serial_number, "x"),
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "passphrase_protected": bool(req.passphrase),
        "created": now.isoformat(),
    }
    (d / "meta.json").write_text(json.dumps(info))
    return info


@router.get("/signing/certificates/{cert_id}")
async def get_certificate(cert_id: str):
    _validate_uuid(cert_id, "certificate ID")
    return _cert_info(cert_id)


@router.get("/signing/certificates/{cert_id}/download")
async def download_certificate(cert_id: str):
    """Public certificate (PEM) so recipients can choose to trust it."""
    from fastapi.responses import Response

    _validate_uuid(cert_id, "certificate ID")
    _cert_info(cert_id)
    pem = (_certs_dir() / cert_id / "cert.pem").read_bytes()
    return Response(pem, media_type="application/x-pem-file",
                    headers={"Content-Disposition": f'attachment; filename="{cert_id}.pem"'})


@router.delete("/signing/certificates/{cert_id}")
async def delete_certificate(cert_id: str):
    _validate_uuid(cert_id, "certificate ID")
    _cert_info(cert_id)
    shutil.rmtree(_certs_dir() / cert_id)
    return {"status": "deleted"}


# ─── Digital signing ─────────────────────────────────────────────────────────


class DigitalSignRequest(BaseModel):
    cert_id: str
    passphrase: Optional[str] = None
    page: int = 0
    rect: Optional[list[float]] = None  # visible frame; None => invisible signature
    image: Optional[str] = None  # base64 PNG shown in the visible field
    reason: Optional[str] = Field(None, max_length=300)
    location: Optional[str] = Field(None, max_length=200)
    contact: Optional[str] = Field(None, max_length=200)
    field_name: Optional[str] = Field(None, max_length=100)  # sign an existing empty field
    show_details: bool = True  # print "Digitally signed by ... / date" in the field
    lock: bool = False  # certify with DocMDP "no changes allowed"


def _local_trust_roots() -> list:
    from pyhanko.keys import load_cert_from_pemder

    roots = []
    d = _certs_dir()
    for sub in d.iterdir():
        pem = sub / "cert.pem"
        if pem.exists():
            try:
                roots.append(load_cert_from_pemder(str(pem)))
            except Exception:
                pass
    return roots


@router.post("/{doc_id}/sign/digital")
async def digital_sign(doc_id: str, req: DigitalSignRequest):
    from pyhanko.pdf_utils.images import PdfImage
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.layout import AxisAlignment, Margins, SimpleBoxLayoutRule
    from pyhanko.pdf_utils.text import TextBoxStyle
    from pyhanko.sign import fields, signers
    from pyhanko.stamp import TextStampStyle

    file_path = _doc_path(doc_id)
    _validate_uuid(req.cert_id, "certificate ID")
    info = _cert_info(req.cert_id)
    cdir = _certs_dir() / req.cert_id
    if info.get("passphrase_protected") and not req.passphrase:
        raise HTTPException(status_code=400, detail="This certificate needs its passphrase")
    try:
        signer = signers.SimpleSigner.load(
            str(cdir / "key.pem"), str(cdir / "cert.pem"),
            key_passphrase=req.passphrase.encode() if req.passphrase else None,
        )
    except Exception:
        signer = None
    if signer is None:
        raise HTTPException(status_code=403, detail="Could not unlock the signing key (wrong passphrase?)")

    # Geometry + field naming, done with PyMuPDF.
    doc = fitz.open(str(file_path))
    try:
        existing_sig_fields = {}
        for p in doc:
            for w in p.widgets() or []:
                if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
                    existing_sig_fields[w.field_name] = bool(w.is_signed)
        already_signed = any(existing_sig_fields.values())
        field_name = (req.field_name or "").strip() or None
        box = None
        if field_name and field_name in existing_sig_fields:
            if existing_sig_fields[field_name]:
                raise HTTPException(status_code=409, detail=f"Field '{field_name}' is already signed")
            new_field = False
        else:
            new_field = True
            if not field_name:
                n = 1
                while f"Signature{n}" in existing_sig_fields:
                    n += 1
                field_name = f"Signature{n}"
            page = _get_page(doc, req.page)
            if req.rect is not None:
                r = _check_rect(page, req.rect)
                pdf_r = (_to_unrotated(page, r) * page.transformation_matrix).normalize()
                box = (pdf_r.x0, pdf_r.y0, pdf_r.x1, pdf_r.y1)
        needs_rewrite = doc.is_encrypted or doc.needs_pass
    finally:
        doc.close()
    if needs_rewrite:
        raise HTTPException(status_code=400, detail="Encrypted PDFs must be decrypted before signing")

    # Visible appearance
    stamp_text = ""
    if req.show_details:
        stamp_text = "Digitally signed by %(signer)s\nDate: %(ts)s"
        if req.reason:
            stamp_text += "\nReason: " + req.reason.replace("%", "%%")
    background = None
    if req.image:
        png = _decode_png(req.image)
        background = PdfImage(Image.open(io.BytesIO(png)))
    style = TextStampStyle(
        stamp_text=stamp_text,
        background=background,
        background_opacity=1.0,
        border_width=0,
        background_layout=SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MID, y_align=AxisAlignment.ALIGN_MAX,
            margins=Margins(2, 2, 2, 2),
        ),
        inner_content_layout=SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MIN, y_align=AxisAlignment.ALIGN_MIN,
            margins=Margins(2, 2, 1, 1),
        ),
        text_box_style=TextBoxStyle(font_size=6),
        timestamp_format="%Y-%m-%d %H:%M:%S %Z",
    )

    meta_kwargs = dict(
        field_name=field_name,
        reason=req.reason or None,
        location=req.location or None,
        contact_info=req.contact or None,
        name=info.get("name"),
    )
    if req.lock:
        if already_signed:
            raise HTTPException(status_code=409,
                                detail="Cannot certify (lock) a document that is already signed")
        from pyhanko.sign.fields import MDPPerm
        meta_kwargs.update(certify=True, docmdp_permissions=MDPPerm.NO_CHANGES)
    meta = signers.PdfSignatureMetadata(**meta_kwargs)

    snapshot(doc_id, f"Digital signature ({info.get('name')})")

    async def _sign_from(src_bytes: bytes) -> bytes:
        w = IncrementalPdfFileWriter(io.BytesIO(src_bytes), strict=False)
        if new_field:
            fields.append_signature_field(
                w, fields.SigFieldSpec(sig_field_name=field_name, on_page=req.page, box=box)
            )
        pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=style)
        out = io.BytesIO()
        await pdf_signer.async_sign_pdf(w, output=out)
        return out.getvalue()

    original = file_path.read_bytes()
    try:
        signed = await _sign_from(original)
    except Exception as first_err:  # noqa: BLE001
        if already_signed:
            raise HTTPException(status_code=422, detail=f"pyHanko could not sign this PDF: {first_err}")
        # Repair through MuPDF (safe: no prior signature to preserve), then retry.
        d = fitz.open(stream=original, filetype="pdf")
        repaired = d.tobytes(garbage=1)
        d.close()
        try:
            signed = await _sign_from(repaired)
        except Exception as err:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"pyHanko could not sign this PDF: {err}")

    tmp = str(file_path) + ".sign.tmp"
    Path(tmp).write_bytes(signed)
    os.replace(tmp, str(file_path))
    return {
        "status": "ok",
        "field_name": field_name,
        "signer": info.get("name"),
        "certified": bool(req.lock),
        "visible": box is not None or not new_field,
    }


# ─── Validation ──────────────────────────────────────────────────────────────


def _name_attr(cert, key: str) -> Optional[str]:
    try:
        return cert.subject.native.get(key)
    except Exception:
        return None


async def _validate_bytes(data: bytes) -> dict:
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import async_validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    try:
        reader = PdfFileReader(io.BytesIO(data), strict=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Not a readable PDF: {e}")

    local_roots = _local_trust_roots()
    local_fps = {c.sha256 for c in local_roots}
    results = []
    for sig in reader.embedded_signatures:
        entry: dict = {"field_name": sig.field_name}
        try:
            vc = ValidationContext(trust_roots=local_roots, allow_fetching=False)
            st = await async_validate_pdf_signature(sig, vc)
            cert = st.signing_cert
            mod_level = getattr(st, "modification_level", None)
            coverage = getattr(st, "coverage", None)
            sig_dt = st.signer_reported_dt
            entry.update({
                "signer_name": _name_attr(cert, "common_name"),
                "signer_email": _name_attr(cert, "email_address"),
                "signer_organization": _name_attr(cert, "organization_name"),
                "issuer": cert.issuer.human_friendly,
                "self_signed": cert.self_signed in ("yes", "maybe", True),
                "issued_by_this_app": cert.sha256 in local_fps,
                "signing_time": sig_dt.isoformat() if sig_dt else None,
                "intact": bool(st.intact),
                "valid": bool(st.valid),
                "trusted": bool(st.trusted),
                "coverage": coverage.name.lower() if coverage is not None else None,
                "modification_level": mod_level.name.lower() if mod_level is not None else None,
                "docmdp_ok": getattr(st, "docmdp_ok", None),
                "certified": bool(sig.sig_object.get("/Reference")) if hasattr(sig, "sig_object") else None,
                "reason": str(sig.sig_object.get("/Reason")) if sig.sig_object.get("/Reason") else None,
                "location": str(sig.sig_object.get("/Location")) if sig.sig_object.get("/Location") else None,
            })
            cov_name = entry["coverage"] or ""
            modified = (not entry["intact"]) or cov_name != "entire_file"
            entry["modified_after_signing"] = modified
            if not entry["intact"]:
                summary = "INVALID: the document was altered after it was signed"
            elif not entry["valid"]:
                summary = "INVALID: the cryptographic signature does not verify"
            elif cov_name != "entire_file" and entry["modification_level"] not in ("none", "lta_updates"):
                summary = (f"Signature valid for the signed revision, but the document was changed "
                           f"afterwards ({entry['modification_level']})")
                if entry["docmdp_ok"] is False:
                    summary = "INVALID: changes after signing violate the certification lock"
            else:
                summary = "Valid: document unchanged since signing"
            if entry["valid"] and entry["intact"] and not entry["trusted"]:
                summary += " (signer identity not trusted: self-signed / unknown issuer)"
            entry["summary"] = summary
        except Exception as e:  # noqa: BLE001
            entry.update({"intact": False, "valid": False, "trusted": False,
                          "modified_after_signing": None, "summary": f"Could not validate: {e}"})
        results.append(entry)

    empty_fields = []
    try:
        d = fitz.open(stream=data, filetype="pdf")
        for pno, p in enumerate(d):
            for w in p.widgets() or []:
                if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and not w.is_signed:
                    vis = fitz.Rect(w.rect) * p.rotation_matrix if p.rotation else fitz.Rect(w.rect)
                    empty_fields.append({"field_name": w.field_name, "page": pno,
                                         "rect": list(fitz.Rect(vis).normalize())})
        d.close()
    except Exception:
        pass

    return {"signature_count": len(results), "signatures": results, "empty_signature_fields": empty_fields}


@router.get("/{doc_id}/sign/validate")
async def validate_document_signatures(doc_id: str):
    file_path = _doc_path(doc_id)
    return await _validate_bytes(file_path.read_bytes())


@router.post("/signing/validate")
async def validate_uploaded_signatures(file: UploadFile = File(...)):
    data = await file.read()
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File is empty or too large")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Not a PDF file")
    return await _validate_bytes(data)
