"""
Forms: fill AND author interactive PDF forms (Acrobat "Prepare Form" parity).

Coordinate convention for every rect in this API
------------------------------------------------
``rect`` is ``[x0, y0, x1, y1]`` in PDF points, **top-left origin, in the page
as displayed** (i.e. after the page's /Rotate is applied -- the same space the
page image is rendered in).  For unrotated pages (the vast majority) this is
identical to PyMuPDF's page coordinates.  Internally widgets live in unrotated
page space; this module converts with ``page.rotation_matrix`` /
``page.derotation_matrix``.  ``pdf_rect`` in field info is the raw unrotated
PyMuPDF rect, for debugging.

Field identity
--------------
Each widget (one visual box) is identified by its PDF object number (``id`` =
xref).  Saves never compact the xref table (garbage<=1), so ids stay stable
across edits made by this module.  Several widgets can share one field
``name`` (radio groups, mirrored fields); fill/import works by name.

Endpoints (all under /api/pdf):
  GET    /{doc_id}/form-fields                 list widgets (optional ?page=)
  POST   /{doc_id}/form-fields                 create a field
  PATCH  /{doc_id}/form-fields/{field_id}      edit field properties
  DELETE /{doc_id}/form-fields/{field_id}      delete a widget (?whole_field=true for all same-name widgets)
  POST   /{doc_id}/form-fields/fill            fill values {values: {name: value}}
  POST   /{doc_id}/form-fields/flatten         bake values into page content, remove widgets
  POST   /{doc_id}/form-fields/detect          auto-detect fields on flat forms
  GET    /{doc_id}/form-fields/export          ?format=json|fdf|xfdf
  POST   /{doc_id}/form-fields/import          {format, data}
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Union

import fitz  # PyMuPDF
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf", tags=["forms"])

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# PDF field flag bits (PDF 32000-1:2008, tables 221/226/228/230)
FF_READONLY = 1
FF_REQUIRED = 2
FF_MULTILINE = 4096
FF_NOTOGGLETOOFF = 16384
FF_RADIO = 32768
FF_COMBO = 131072
FF_EDIT = 262144
FF_MULTISELECT = 2097152

# Acrobat's standard date format actions (AFDate_* live in every viewer's
# built-in JavaScript), used for fields auto-detected next to "Date"/"DOB".
DATE_FORMAT = "mm/dd/yyyy"

TYPE_NAMES = {
    fitz.PDF_WIDGET_TYPE_BUTTON: "button",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "combo",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "list",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    fitz.PDF_WIDGET_TYPE_TEXT: "text",
}
TYPE_CODES = {v: k for k, v in TYPE_NAMES.items()}
CREATABLE_TYPES = ("text", "checkbox", "radio", "combo", "list", "signature")

_TRUE_STRINGS = {"true", "yes", "on", "1", "x", "checked"}
_FALSE_STRINGS = {"false", "no", "off", "0", "", "unchecked"}


# ─── Low-level PDF helpers ───────────────────────────────────────────────────


def _pdf_name(s: str) -> str:
    """Encode a string as a PDF name token (without the leading slash)."""
    out = []
    for ch in s:
        o = ord(ch)
        if o < 33 or o > 126 or ch in "()<>[]{}/%#":
            for b in ch.encode("utf-8"):
                out.append("#%02X" % b)
        else:
            out.append(ch)
    return "".join(out) or "Off"


def _pdf_str(s: str) -> str:
    """Encode a Python string as a PDF string literal."""
    try:
        s.encode("latin-1")
        esc = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        esc = esc.replace("\r", "\\r").replace("\n", "\\n")
        return f"({esc})"
    except UnicodeEncodeError:
        return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _key_as_pdf(doc: fitz.Document, xref: int, key: str) -> Optional[str]:
    """Return a key's value re-serialised as PDF source, or None if absent."""
    typ, val = doc.xref_get_key(xref, key)
    if typ == "null":
        return None
    if typ == "string":
        return _pdf_str(val)
    return val


def _ref_xref(val: str) -> int:
    m = re.match(r"\s*(\d+)\s+\d+\s+R", val or "")
    return int(m.group(1)) if m else 0


def _parent_xref(doc: fitz.Document, xref: int) -> int:
    typ, val = doc.xref_get_key(xref, "Parent")
    return _ref_xref(val) if typ == "xref" else 0


def _acroform_fields(doc: fitz.Document) -> tuple[int, list[int]]:
    """Return (holder_xref, field xrefs) where holder_xref owns the Fields array.

    holder is the catalog when /AcroForm/Fields is inline, else the xref of the
    indirect array object.
    """
    cat = doc.pdf_catalog()
    typ, val = doc.xref_get_key(cat, "AcroForm/Fields")
    if typ == "xref":
        arr_xref = _ref_xref(val)
        return arr_xref, [int(x) for x in re.findall(r"(\d+)\s+0\s+R", doc.xref_object(arr_xref))]
    if typ == "array":
        return cat, [int(x) for x in re.findall(r"(\d+)\s+0\s+R", val)]
    return cat, []


def _set_acroform_fields(doc: fitz.Document, xrefs: list[int]):
    cat = doc.pdf_catalog()
    arr = "[" + " ".join(f"{x} 0 R" for x in xrefs) + "]"
    typ, val = doc.xref_get_key(cat, "AcroForm/Fields")
    if typ == "xref":
        doc.update_object(_ref_xref(val), arr)
    else:
        if doc.xref_get_key(cat, "AcroForm")[0] == "null":
            doc.xref_set_key(cat, "AcroForm", "<< >>")
        doc.xref_set_key(cat, "AcroForm/Fields", arr)


def _ap_states(doc: fitz.Document, xref: int) -> list[str]:
    typ, val = doc.xref_get_key(xref, "AP/N")
    if typ != "dict":
        return []
    return re.findall(r"/([^\s/<>\[\]()]+)\s+\d+\s+0\s+R", val)


def _unrot_to_pdf(page: fitz.Page) -> fitz.Matrix:
    """Matrix mapping unrotated MuPDF page coords -> raw PDF user space."""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    m = ~page.transformation_matrix
    if rot:
        page.set_rotation(rot)
    return m


def _display_rect(page: fitz.Page, r: fitz.Rect) -> list[float]:
    d = (fitz.Rect(r) * page.rotation_matrix).normalize()
    return [round(d.x0, 2), round(d.y0, 2), round(d.x1, 2), round(d.y1, 2)]


def _to_unrotated(page: fitz.Page, rect: list[float]) -> fitz.Rect:
    if not rect or len(rect) != 4:
        raise HTTPException(status_code=400, detail="rect must be [x0, y0, x1, y1]")
    r = fitz.Rect(rect).normalize()
    if r.width < 2 or r.height < 2:
        raise HTTPException(status_code=400, detail="rect is too small (min 2x2 pt)")
    u = (r * page.derotation_matrix).normalize()
    bounds = fitz.Rect(0, 0, page.cropbox.width, page.cropbox.height)
    if not u.intersects(bounds):
        raise HTTPException(status_code=400, detail="rect is outside the page")
    return u


# ─── Document I/O ────────────────────────────────────────────────────────────


def _doc_path(doc_id: str) -> Path:
    if not _UUID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    path = UPLOAD_DIR / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


@contextmanager
def _open_doc(doc_id: str, operation: Optional[str] = None):
    """Open the document; when ``operation`` is given, save it afterwards.

    snapshot() is taken right before the file on disk is replaced, so undo
    restores exactly the pre-operation state and failed operations (which
    raise before this point) never pollute history.
    """
    path = _doc_path(doc_id)
    doc = fitz.open(str(path))
    try:
        if doc.needs_pass:
            raise HTTPException(status_code=400, detail="Document is encrypted; unlock it first")
        if not doc.is_pdf:
            raise HTTPException(status_code=400, detail="Not a PDF")
        yield doc
        if operation:
            data = doc.tobytes(garbage=1, deflate=True)
            snapshot(doc_id, operation)
            tmp = path.with_suffix(".forms.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
    finally:
        doc.close()


def _check_page(doc: fitz.Document, page: int) -> fitz.Page:
    if page < 0 or page >= doc.page_count:
        raise HTTPException(status_code=400, detail=f"Page {page} out of range (0-{doc.page_count - 1})")
    return doc[page]


# ─── Reading fields ──────────────────────────────────────────────────────────


def _iter_widgets(doc: fitz.Document, page: Optional[int] = None):
    pages = [page] if page is not None else range(doc.page_count)
    for pno in pages:
        pg = doc[pno]
        for w in pg.widgets():
            # Widget.parent is a weakref to the page; keep the page alive for
            # as long as the widget object is referenced.
            w._keep_page = pg
            yield pg, w


def _find_widget(doc: fitz.Document, field_id: int):
    for pg, w in _iter_widgets(doc):
        if w.xref == field_id:
            return pg, w
    raise HTTPException(status_code=404, detail=f"Field {field_id} not found")


def _is_on(doc: fitz.Document, xref: int) -> bool:
    typ, val = doc.xref_get_key(xref, "AS")
    return typ == "name" and val not in ("/Off", "")


def _on_state(w: fitz.Widget) -> str:
    try:
        st = w.on_state()
    except Exception:
        st = None
    if not st or st is True:
        return "Yes"
    return str(st)


def _norm_options(values) -> tuple[list[str], list[str]]:
    opts, labels = [], []
    for v in values or []:
        if isinstance(v, (list, tuple)) and v:
            opts.append(str(v[0]))
            labels.append(str(v[1]) if len(v) > 1 else str(v[0]))
        else:
            opts.append(str(v))
            labels.append(str(v))
    return opts, labels


def _choice_values_raw(doc: fitz.Document, w: fitz.Widget) -> list[str]:
    """Selected value(s) of a choice field read from /V (string or array).

    PyMuPDF reports an array /V (multi-select list box) as ''."""
    holder = _field_holder(doc, w)
    typ, val = doc.xref_get_key(holder, "V")
    if typ == "string":
        return [val] if val else []
    if typ == "array":
        out, i = [], 0
        val = val.strip()[1:-1]
        while i < len(val):
            if val[i] in "(<":
                t, i = _parse_pdf_string_at(val, i)
                out.append(t)
            else:
                i += 1
        return out
    if typ == "name" and val not in ("/Off", ""):
        return [val[1:]]
    return []


def _field_format(doc: fitz.Document, w: fitz.Widget) -> Optional[str]:
    try:
        holder = _field_holder(doc, w)
        for x in (holder, w.xref):
            typ, js = doc.xref_get_key(x, "AA/F/JS")
            if typ == "string" and "AFDate_" in js:
                return "date"
    except Exception:
        pass
    return None


def _set_date_format(doc: fitz.Document, xref: int, fmt: str = DATE_FORMAT):
    k = _pdf_str(f'AFDate_KeystrokeEx("{fmt}");')
    f = _pdf_str(f'AFDate_FormatEx("{fmt}");')
    doc.xref_set_key(xref, "AA", f"<< /K << /S /JavaScript /JS {k} >> /F << /S /JavaScript /JS {f} >> >>")


def _clear_format(doc: fitz.Document, xref: int):
    if doc.xref_get_key(xref, "AA")[0] != "null":
        doc.xref_set_key(xref, "AA", "null")


_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _normalize_date(text: str) -> str:
    """ISO dates (what <input type=date> sends) -> mm/dd/yyyy; else unchanged."""
    m = _ISO_DATE.match(text.strip())
    if not m:
        return text
    y, mo, d = m.groups()
    return f"{int(mo):02d}/{int(d):02d}/{y}"


def _listbox_appearance(doc: fitz.Document, w: fitz.Widget, selected: list[str]):
    """Regenerate a list box appearance that highlights every selected row.

    PyMuPDF's own appearance draws the option texts only (no selection).
    """
    try:
        typ, ref = doc.xref_get_key(w.xref, "AP/N")
        if typ != "xref":
            return
        ap = int(ref.split()[0])
        r = fitz.Rect(w.rect)
        W, H = r.width, r.height
        opts, labels = _norm_options(w.choice_values)
        fs = float(w.text_fontsize or 0) or 12.0
        lh = fs * 1.116
        ops = ["/Tx BMC", "q", "1 w", f"1 1 {W - 2:.3f} {H - 2:.3f} re", "W", "n"]
        for i, o in enumerate(opts):
            if o in selected:
                y = H - 2 - (i + 1) * lh
                ops.append(f"0.6 0.75 0.95 rg 1 {y:.3f} {W - 2:.3f} {lh:.3f} re f")
        ops += ["BT", "0 0 0 rg", f"2 {H:.3f} Td"]
        for i, lab in enumerate(labels):
            try:
                lab.encode("latin-1")
                s = lab
            except UnicodeEncodeError:
                s = lab.encode("latin-1", "replace").decode("latin-1")
            ops.append(f"0 {-lh:.3f} Td /Helv {fs:g} Tf {_pdf_str(s)} Tj")
        ops += ["ET", "Q", "EMC"]
        doc.update_stream(ap, "\n".join(ops).encode("latin-1"))
    except Exception:
        pass


def _set_listbox(doc: fitz.Document, w: fitz.Widget, values: list[str]):
    """Select ``values`` in a list box (several only if it is multi-select)."""
    opts, _labels = _norm_options(w.choice_values)
    w.field_value = values[0] if values else ""
    w.update()
    holder = _field_holder(doc, w)
    if len(values) > 1:
        doc.xref_set_key(holder, "V", "[" + " ".join(_pdf_str(v) for v in values) + "]")
    elif not values:
        doc.xref_set_key(holder, "V", "null")
    idx = sorted(opts.index(v) for v in values if v in opts)
    doc.xref_set_key(holder, "I", "[" + " ".join(map(str, idx)) + "]" if idx else "null")
    _listbox_appearance(doc, w, values)


def _widget_info(doc: fitz.Document, page: fitz.Page, w: fitz.Widget) -> dict:
    ftype = TYPE_NAMES.get(w.field_type, "unknown")
    flags = int(w.field_flags or 0)
    info: dict[str, Any] = {
        "format": _field_format(doc, w),
        "id": w.xref,
        "page": page.number,
        "name": w.field_name or "",
        "type": ftype,
        "rect": _display_rect(page, w.rect),
        "pdf_rect": [round(v, 2) for v in w.rect],
        "required": bool(flags & FF_REQUIRED),
        "readonly": bool(flags & FF_READONLY),
        "tooltip": w.field_label or "",
        "font_size": float(w.text_fontsize or 0),
        "multiline": bool(flags & FF_MULTILINE) if ftype == "text" else False,
        "max_len": int(w.text_maxlen or 0) if ftype == "text" else 0,
        "options": [],
        "option_labels": [],
        "export_value": None,
        "value": None,
    }
    if ftype == "text":
        info["value"] = w.field_value or ""
    elif ftype == "checkbox":
        info["export_value"] = _on_state(w)
        info["value"] = _is_on(doc, w.xref)
    elif ftype == "radio":
        info["export_value"] = _on_state(w)
        info["checked"] = _is_on(doc, w.xref)
    elif ftype in ("combo", "list"):
        opts, labels = _norm_options(w.choice_values)
        info["options"], info["option_labels"] = opts, labels
        info["value"] = w.field_value if w.field_value not in (None, "Off") else ""
        info["editable"] = bool(flags & FF_EDIT) if ftype == "combo" else False
        if ftype == "list":
            multi = bool(flags & FF_MULTISELECT)
            info["multi_select"] = multi
            sel = _choice_values_raw(doc, w)
            if multi:
                info["value"] = sel
            elif sel:
                info["value"] = sel[0]
    elif ftype == "signature":
        info["value"] = doc.xref_get_key(w.xref, "V")[0] != "null" or (
            _parent_xref(doc, w.xref) and doc.xref_get_key(_parent_xref(doc, w.xref), "V")[0] != "null"
        )
        info["value"] = bool(info["value"])
    return info


def _list_fields(doc: fitz.Document, page: Optional[int] = None) -> list[dict]:
    # Radio group value/options need the whole document, not just one page.
    all_infos = [_widget_info(doc, pg, w) for pg, w in _iter_widgets(doc)]
    groups: dict[str, list[dict]] = {}
    for f in all_infos:
        if f["type"] == "radio":
            groups.setdefault(f["name"], []).append(f)
    for name, members in groups.items():
        opts: list[str] = []
        for m in members:
            if m["export_value"] not in opts:
                opts.append(m["export_value"])
        selected = next((m["export_value"] for m in members if m.get("checked")), None)
        for m in members:
            m["options"] = list(opts)
            m["option_labels"] = list(opts)
            m["value"] = selected
    if page is not None:
        return [f for f in all_infos if f["page"] == page]
    return all_infos


def _values_by_name(fields: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        if f["type"] in ("signature", "button", "unknown") or not f["name"]:
            continue
        if f["type"] == "checkbox":
            # Same-name checkboxes with different export values behave like a
            # radio group: store the export value of the checked one.
            prev = out.get(f["name"])
            exports = {g["export_value"] for g in fields if g["name"] == f["name"]}
            if len(exports) > 1:
                if f["value"]:
                    out[f["name"]] = f["export_value"]
                elif prev is None:
                    out[f["name"]] = None
            else:
                out[f["name"]] = bool(f["value"]) or bool(prev)
        else:
            out.setdefault(f["name"], f["value"])
    return out


# ─── Filling ─────────────────────────────────────────────────────────────────


def _truthy(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE_STRINGS:
        return True
    if s in _FALSE_STRINGS:
        return False
    return None  # not a boolean-ish string (maybe an export value)


def _set_radio_group(doc: fitz.Document, widgets: list[fitz.Widget], value: Any):
    target = None if value in (None, False, "", "Off") else str(value)
    states = {_on_state(w) for w in widgets}
    if target is not None and target not in states:
        raise ValueError(f"'{target}' is not one of the radio options {sorted(states)}")
    parents = set()
    for w in widgets:
        on = _on_state(w)
        doc.xref_set_key(w.xref, "AS", "/" + (_pdf_name(on) if on == target else "Off"))
        par = _parent_xref(doc, w.xref)
        if par:
            parents.add(par)
        else:
            doc.xref_set_key(w.xref, "V", "/" + (_pdf_name(target) if target else "Off"))
    for par in parents:
        doc.xref_set_key(par, "V", "/" + (_pdf_name(target) if target else "Off"))


def _fill_values(doc: fitz.Document, values: dict[str, Any], force_readonly: bool = False) -> tuple[list[str], dict[str, str]]:
    by_name: dict[str, list[fitz.Widget]] = {}
    for _pg, w in _iter_widgets(doc):
        if w.field_name:
            by_name.setdefault(w.field_name, []).append(w)

    filled: list[str] = []
    errors: dict[str, str] = {}
    for name, value in values.items():
        widgets = by_name.get(name)
        if not widgets:
            errors[name] = "no such field"
            continue
        ftype = TYPE_NAMES.get(widgets[0].field_type, "unknown")
        if (int(widgets[0].field_flags or 0) & FF_READONLY) and not force_readonly:
            errors[name] = "field is read-only"
            continue
        try:
            if ftype == "text":
                text = "" if value is None else str(value)
                if _field_format(doc, widgets[0]) == "date":
                    text = _normalize_date(text)
                for w in widgets:
                    if w.text_maxlen and len(text) > w.text_maxlen:
                        raise ValueError(f"value longer than max length {w.text_maxlen}")
                for w in widgets:
                    w.field_value = text
                    w.update()
            elif ftype == "checkbox":
                exports = {_on_state(w) for w in widgets}
                tv = _truthy(value)
                for w in widgets:
                    on = _on_state(w)
                    if isinstance(value, str) and value in exports and len(exports) > 1:
                        want = value == on
                    elif isinstance(value, str) and value == on:
                        want = True
                    elif tv is None:
                        raise ValueError(f"cannot interpret {value!r} as checked/unchecked (export value is {on!r})")
                    else:
                        want = tv
                    w.field_value = on if want else "Off"
                    w.update()
            elif ftype == "radio":
                if isinstance(value, bool):
                    raise ValueError("radio groups take an export value, not a boolean")
                _set_radio_group(doc, widgets, value)
            elif ftype in ("combo", "list"):
                w0 = widgets[0]
                opts, labels = _norm_options(w0.choice_values)
                editable = bool(int(w0.field_flags or 0) & FF_EDIT)
                vals = value if isinstance(value, list) else [value]
                vals = ["" if v is None else str(v) for v in vals]
                multi = bool(int(w0.field_flags or 0) & FF_MULTISELECT)
                if ftype == "list" and len([v for v in vals if v != ""]) > 1 and not multi:
                    raise ValueError("this list box allows only one selection")
                resolved = []
                for v in vals:
                    if v in opts or v == "":
                        resolved.append(v)
                    elif v in labels:
                        resolved.append(opts[labels.index(v)])
                    elif ftype == "combo" and editable:
                        resolved.append(v)
                    else:
                        raise ValueError(f"'{v}' is not one of the options {opts}")
                if ftype == "list":
                    chosen = [v for v in resolved if v != ""]
                    seen: list[str] = []
                    for v in chosen:
                        if v not in seen:
                            seen.append(v)
                    for w in widgets:
                        _set_listbox(doc, w, seen)
                else:
                    for w in widgets:
                        w.field_value = resolved[0] if resolved else ""
                        w.update()
            elif ftype == "signature":
                raise ValueError("signature fields cannot be filled with a value")
            else:
                raise ValueError(f"unsupported field type {ftype}")
            filled.append(name)
        except ValueError as e:
            errors[name] = str(e)
    return filled, errors


# ─── Creating / editing ──────────────────────────────────────────────────────


def _existing_names(doc: fitz.Document) -> set[str]:
    return {w.field_name for _pg, w in _iter_widgets(doc) if w.field_name}


def _unique_name(base: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_\-]+", "_", base).strip("_")[:40] or "field"
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def _rewrite_radio_kid(doc: fitz.Document, kid: int, parent: int, export: str, page_xref: int):
    """Turn a stand-alone radio widget made by PyMuPDF into a kid of `parent`.

    PyMuPDF's add_widget creates every radio button as its own terminal field
    with on-state 'Yes', so buttons never act as a group.  Rewrite the widget
    dict cleanly: no /T /FT /Ff /V (inherited from parent), /Parent set, and
    the on appearance renamed to the export value.
    """
    states = _ap_states(doc, kid)
    on_key = next((s for s in states if s != "Off"), None)
    off_ref = doc.xref_get_key(kid, "AP/N/Off")[1]
    on_ref = doc.xref_get_key(kid, f"AP/N/{on_key}")[1] if on_key else None
    parts = ["/Type /Annot", "/Subtype /Widget"]
    for key in ("Rect", "F", "DA", "MK", "BS", "TU"):
        v = _key_as_pdf(doc, kid, key)
        if v is not None:
            parts.append(f"/{key} {v}")
    parts.append(f"/P {page_xref} 0 R")
    parts.append(f"/Parent {parent} 0 R")
    parts.append("/AS /Off")
    ap = f"/Off {off_ref}" + (f" /{_pdf_name(export)} {on_ref}" if on_ref else "")
    parts.append(f"/AP << /N << {ap} >> >>")
    doc.update_object(kid, "<< " + " ".join(parts) + " >>")


def _radio_parent_for(doc: fitz.Document, name: str, flags: int, tooltip: Optional[str]) -> int:
    """Find (or create) the parent field object of radio group `name`."""
    members = [(pg, w) for pg, w in _iter_widgets(doc)
               if w.field_name == name and w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON]
    for _pg, w in members:
        par = _parent_xref(doc, w.xref)
        if par:
            return par
    holder, fields = _acroform_fields(doc)
    par = doc.get_new_xref()
    tu = f" /TU {_pdf_str(tooltip)}" if tooltip else ""
    doc.update_object(par, f"<< /FT /Btn /Ff {flags} /T {_pdf_str(name)} /V /Off /Kids []{tu} >>")
    # Adopt pre-existing stand-alone radio widgets of the same name.
    kids = []
    for pg, w in members:
        export = _on_state(w)
        was_on = _is_on(doc, w.xref)
        _rewrite_radio_kid(doc, w.xref, par, export, pg.xref)
        if was_on:
            doc.xref_set_key(w.xref, "AS", "/" + _pdf_name(export))
            doc.xref_set_key(par, "V", "/" + _pdf_name(export))
        kids.append(w.xref)
    fields = [x for x in fields if x not in kids] + [par]
    _set_acroform_fields(doc, fields)
    doc.xref_set_key(par, "Kids", "[" + " ".join(f"{k} 0 R" for k in kids) + "]")
    return par


def _add_radio_kid(doc: fitz.Document, page: fitz.Page, rect: fitz.Rect, name: str, export: str,
                   flags: int, tooltip: Optional[str]) -> int:
    par = _radio_parent_for(doc, name, flags, tooltip)
    existing = [w for _pg, w in _iter_widgets(doc) if w.field_name == name]
    if any(_on_state(w) == export for w in existing):
        raise HTTPException(status_code=409, detail=f"Radio group '{name}' already has option '{export}'")
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_RADIOBUTTON
    w.field_name = name + "__tmp_radio"
    w.field_value = False
    w.rect = rect
    w.border_color = (0, 0, 0)
    w.border_width = 1
    annot = page.add_widget(w)
    kid = annot.xref
    _rewrite_radio_kid(doc, kid, par, export, page.xref)
    holder, fields = _acroform_fields(doc)
    _set_acroform_fields(doc, [x for x in fields if x != kid])
    kids = re.findall(r"(\d+)\s+0\s+R", doc.xref_get_key(par, "Kids")[1] or "")
    doc.xref_set_key(par, "Kids", "[" + " ".join(f"{k} 0 R" for k in kids + [str(kid)]) + "]")
    return kid


class CreateFieldRequest(BaseModel):
    page: int
    type: str  # text | checkbox | radio | combo | list | signature
    rect: list[float]  # display points, top-left origin
    name: Optional[str] = None
    value: Optional[Union[bool, str, list[str]]] = None  # initial/default value
    options: Optional[list[str]] = None  # combo/list choices
    export_value: Optional[str] = None  # checkbox/radio on-state
    font_size: float = 0  # 0 = auto
    required: bool = False
    readonly: bool = False
    tooltip: Optional[str] = None
    multiline: bool = False
    max_len: int = 0
    editable: bool = False  # combo: allow custom text
    multi_select: bool = False  # list: allow several selections
    format: Optional[str] = None  # text: "date" -> Acrobat date format actions


def _create_field(doc: fitz.Document, req: CreateFieldRequest) -> int:
    ftype = req.type.lower()
    if ftype not in CREATABLE_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {CREATABLE_TYPES}")
    page = _check_page(doc, req.page)
    rect = _to_unrotated(page, req.rect)
    taken = _existing_names(doc)
    name = (req.name or "").strip()
    flags = (FF_REQUIRED if req.required else 0) | (FF_READONLY if req.readonly else 0)

    if ftype == "radio":
        if not name:
            name = _unique_name("radio_group", taken)
        elif name in taken:
            others = [w for _pg, w in _iter_widgets(doc) if w.field_name == name]
            if any(w.field_type != fitz.PDF_WIDGET_TYPE_RADIOBUTTON for w in others):
                raise HTTPException(status_code=409, detail=f"Field name '{name}' is used by a non-radio field")
        export = (req.export_value or "").strip()
        if not export:
            n = sum(1 for _pg, w in _iter_widgets(doc) if w.field_name == name)
            export = f"Choice{n + 1}"
        kid = _add_radio_kid(doc, page, rect, name, export, FF_RADIO | FF_NOTOGGLETOOFF | flags, req.tooltip)
        if req.value is True or (isinstance(req.value, str) and req.value == export):
            members = [w for _pg, w in _iter_widgets(doc) if w.field_name == name]
            _set_radio_group(doc, members, export)
        return kid

    if name and name in taken:
        raise HTTPException(status_code=409, detail=f"A field named '{name}' already exists")
    if not name:
        name = _unique_name(ftype, taken)

    w = fitz.Widget()
    w.field_type = TYPE_CODES[ftype]
    w.field_name = name
    w.rect = rect
    if req.tooltip:
        w.field_label = req.tooltip
    if ftype in ("text", "combo", "list"):
        w.text_fontsize = max(0.0, float(req.font_size or 0))
        w.text_font = "Helv"
    if ftype == "text":
        if req.multiline:
            flags |= FF_MULTILINE
        if req.max_len:
            w.text_maxlen = int(req.max_len)
        w.field_value = "" if req.value in (None, False, True) else str(req.value)
    elif ftype == "checkbox":
        w.border_color = (0, 0, 0)
        w.border_width = 1
        w.field_value = False
    elif ftype in ("combo", "list"):
        opts = [str(o) for o in (req.options or []) if str(o) != ""]
        if not opts:
            raise HTTPException(status_code=400, detail=f"{ftype} fields need at least one option")
        w.choice_values = opts
        if ftype == "combo" and req.editable:
            flags |= FF_EDIT
        if ftype == "list" and req.multi_select:
            flags |= FF_MULTISELECT
        if isinstance(req.value, str) and req.value:
            if req.value not in opts and not (ftype == "combo" and req.editable):
                raise HTTPException(status_code=400, detail=f"default '{req.value}' is not an option")
            w.field_value = req.value
    elif ftype == "signature":
        w.border_color = (0, 0, 0)
        w.border_width = 1
    if ftype == "combo":
        flags |= FF_COMBO
    w.field_flags = flags
    annot = page.add_widget(w)
    xref = annot.xref

    if ftype == "text" and (req.format or "") == "date":
        _set_date_format(doc, xref)
    if ftype == "list" and isinstance(req.value, list) and req.value:
        _fill_values(doc, {name: [str(v) for v in req.value]}, force_readonly=True)
    if ftype == "checkbox":
        export = (req.export_value or "").strip()
        if export and export != "Yes":
            _rename_on_state(doc, xref, export)
        if _truthy(req.value):
            for _pg, ww in _iter_widgets(doc, page.number):
                if ww.xref == xref:
                    ww.field_value = _on_state(ww)
                    ww.update()
    return xref


def _rename_on_state(doc: fitz.Document, xref: int, new: str):
    states = _ap_states(doc, xref)
    old = next((s for s in states if s != "Off"), None)
    if not old or old == _pdf_name(new):
        return
    for sub in ("N", "D"):
        typ, ref = doc.xref_get_key(xref, f"AP/{sub}/{old}")
        if typ != "xref":
            continue
        off = doc.xref_get_key(xref, f"AP/{sub}/Off")
        off_part = f"/Off {off[1]} " if off[0] == "xref" else ""
        doc.xref_set_key(xref, f"AP/{sub}", f"<< {off_part}/{_pdf_name(new)} {ref} >>")
    was_on = _is_on(doc, xref)
    if was_on:
        doc.xref_set_key(xref, "AS", "/" + _pdf_name(new))
        holder = _parent_xref(doc, xref) or xref
        doc.xref_set_key(holder, "V", "/" + _pdf_name(new))


class UpdateFieldRequest(BaseModel):
    name: Optional[str] = None
    rect: Optional[list[float]] = None  # display points
    value: Optional[Union[bool, str, list[str]]] = None
    options: Optional[list[str]] = None
    export_value: Optional[str] = None
    font_size: Optional[float] = None
    required: Optional[bool] = None
    readonly: Optional[bool] = None
    tooltip: Optional[str] = None
    multiline: Optional[bool] = None
    max_len: Optional[int] = None
    editable: Optional[bool] = None
    multi_select: Optional[bool] = None
    format: Optional[str] = None  # "date" | "" (none); text fields only


def _field_holder(doc: fitz.Document, w: fitz.Widget) -> int:
    """xref of the object that holds the field-level keys (T, Ff, V, TU)."""
    par = _parent_xref(doc, w.xref)
    if par and doc.xref_get_key(w.xref, "T")[0] == "null":
        return par
    return w.xref


def _update_field(doc: fitz.Document, field_id: int, req: UpdateFieldRequest):
    page, w = _find_widget(doc, field_id)
    ftype = TYPE_NAMES.get(w.field_type, "unknown")
    is_radio = ftype == "radio"
    holder = _field_holder(doc, w)

    if req.name is not None:
        new = req.name.strip()
        if not new:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if new != w.field_name:
            if new in _existing_names(doc):
                raise HTTPException(status_code=409, detail=f"A field named '{new}' already exists")
            # Rename the whole logical field (all widgets sharing the name).
            old = w.field_name
            holders = {_field_holder(doc, ww) for _pg, ww in _iter_widgets(doc) if ww.field_name == old}
            partial = old.rsplit(".", 1)
            for h in holders:
                # Only the terminal part of a hierarchical name lives in /T.
                doc.xref_set_key(h, "T", _pdf_str(new.rsplit(".", 1)[-1] if len(partial) > 1 else new))

    if req.rect is not None:
        r = _to_unrotated(page, req.rect)
        pdf_r = r * _unrot_to_pdf(page)
        pdf_r.normalize()
        doc.xref_set_key(w.xref, "Rect", f"[{pdf_r.x0:.3f} {pdf_r.y0:.3f} {pdf_r.x1:.3f} {pdf_r.y1:.3f}]")

    flags = int(doc.xref_get_key(holder, "Ff")[1]) if doc.xref_get_key(holder, "Ff")[0] == "int" else 0

    def setbit(bit: int, on: Optional[bool]):
        nonlocal flags
        if on is None:
            return
        flags = (flags | bit) if on else (flags & ~bit)

    setbit(FF_REQUIRED, req.required)
    setbit(FF_READONLY, req.readonly)
    if ftype == "text":
        setbit(FF_MULTILINE, req.multiline)
    if ftype == "combo":
        setbit(FF_EDIT, req.editable)
    if ftype == "list":
        setbit(FF_MULTISELECT, req.multi_select)
    if any(v is not None for v in (req.required, req.readonly, req.multiline, req.editable, req.multi_select)):
        doc.xref_set_key(holder, "Ff", str(flags))
    if ftype == "list" and req.multi_select is False:
        sel = _choice_values_raw(doc, w)
        if len(sel) > 1:  # keep only the first selection
            _p, wl = _find_widget(doc, field_id)
            _set_listbox(doc, wl, sel[:1])
    if req.format is not None and ftype == "text":
        if req.format == "date":
            _set_date_format(doc, holder)
        elif req.format in ("", "none"):
            _clear_format(doc, holder)
        else:
            raise HTTPException(status_code=400, detail="format must be 'date' or ''")

    if req.tooltip is not None:
        doc.xref_set_key(holder, "TU", _pdf_str(req.tooltip) if req.tooltip else "null")

    if req.export_value is not None and ftype in ("checkbox", "radio"):
        new_exp = req.export_value.strip()
        if not new_exp or new_exp == "Off":
            raise HTTPException(status_code=400, detail="export_value cannot be empty or 'Off'")
        if is_radio:
            siblings = [ww for _pg, ww in _iter_widgets(doc) if ww.field_name == w.field_name and ww.xref != w.xref]
            if any(_on_state(s) == new_exp for s in siblings):
                raise HTTPException(status_code=409, detail=f"Another button in the group already uses '{new_exp}'")
        _rename_on_state(doc, w.xref, new_exp)

    # Properties that need the appearance regenerated go through Widget.update().
    # Never call update() on radio kids: PyMuPDF writes /Ff and /AS onto the kid.
    if not is_radio and ftype != "signature":
        page2, w2 = _find_widget(doc, field_id)  # reload after raw edits
        changed = False
        if req.font_size is not None and ftype in ("text", "combo", "list"):
            w2.text_fontsize = max(0.0, float(req.font_size))
            changed = True
        if req.max_len is not None and ftype == "text":
            w2.text_maxlen = max(0, int(req.max_len))
            changed = True
        if req.options is not None and ftype in ("combo", "list"):
            opts = [str(o) for o in req.options if str(o) != ""]
            if not opts:
                raise HTTPException(status_code=400, detail="at least one option is required")
            w2.choice_values = opts
            if w2.field_value not in opts and not (flags & FF_EDIT):
                w2.field_value = opts[0] if ftype == "combo" else ""
            changed = True
        if req.rect is not None or req.multiline is not None:
            changed = True  # regenerate appearance for the new box size / wrapping
        if changed:
            w2.update()

    if req.value is not None:
        _, w3 = _find_widget(doc, field_id)
        filled, errors = _fill_values(doc, {w3.field_name: req.value}, force_readonly=True)
        if errors:
            raise HTTPException(status_code=400, detail=next(iter(errors.values())))


def _delete_widget(doc: fitz.Document, page: fitz.Page, w: fitz.Widget):
    par = _parent_xref(doc, w.xref)
    xref = w.xref
    page.delete_widget(w)
    holder, fields = _acroform_fields(doc)
    fields = [f for f in fields if f != xref]
    if par:
        typ, kids = doc.xref_get_key(par, "Kids")
        if typ == "array":
            remaining = [int(k) for k in re.findall(r"(\d+)\s+0\s+R", kids) if int(k) != xref]
            doc.xref_set_key(par, "Kids", "[" + " ".join(f"{k} 0 R" for k in remaining) + "]")
            if not remaining:
                fields = [f for f in fields if f != par]
    _set_acroform_fields(doc, fields)


# ─── Auto-detect ─────────────────────────────────────────────────────────────

_BOX_GLYPHS = set("☐□❏❑▢◻⬜⃞")


def _overlap_ratio(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = fitz.Rect(a) & fitz.Rect(b)
    if inter.is_empty:
        return 0.0
    small = min(a.get_area(), b.get_area()) or 1.0
    return inter.get_area() / small


def _label_for(rect: fitz.Rect, words: list[tuple], prefer_right: bool = False) -> str:
    """Nearest text label: same row to the left, else just above, else just below.

    Checkboxes (prefer_right) are usually labelled by the text right after them.
    """
    cy = (rect.y0 + rect.y1) / 2
    if prefer_right:
        right = sorted((w for w in words if w[0] >= rect.x1 - 2 and w[1] <= cy <= w[3]
                        and w[4].strip("_")), key=lambda w: w[0])
        # only the FIRST word must be close to the box; the rest follow by word gap
        if right and right[0][0] - rect.x1 < 30:
            run = [right[0]]
            for w in right[1:]:
                if w[0] - run[-1][2] < 12 and len(run) < 5:
                    run.append(w)
                else:
                    break
            return " ".join(w[4] for w in run).strip().rstrip(":")
    left = [w for w in words if w[2] <= rect.x0 + 2 and w[1] <= cy <= w[3] and rect.x0 - w[2] < 220
            and "_" not in w[4].strip("_") and w[4].strip("_")]
    if left:
        left.sort(key=lambda w: w[0])
        # contiguous run of words ending at the nearest one
        run = [left[-1]]
        for w in reversed(left[:-1]):
            if run[0][0] - w[2] < 12:
                run.insert(0, w)
            else:
                break
        txt = " ".join(w[4].strip("_") for w in run[-5:])
        return txt.strip().rstrip(":").strip()
    above = [w for w in words if 0 <= rect.y0 - w[3] < 16 and w[0] < rect.x1 and w[2] > rect.x0 and w[4].strip("_")]
    if above:
        above.sort(key=lambda w: (round(w[1]), w[0]))
        return " ".join(w[4] for w in above[:5]).strip().rstrip(":")
    below = [w for w in words if 0 <= w[1] - rect.y1 < 14 and w[0] < rect.x1 and w[2] > rect.x0 and w[4].strip("_")]
    if below:
        below.sort(key=lambda w: (round(w[1]), w[0]))
        return " ".join(w[4] for w in below[:5]).strip().rstrip(":")
    # Table column: nearest header text further up whose x-range overlaps this cell.
    col = [w for w in words if 0 <= rect.y0 - w[3] < 300 and w[4].strip("_")
           and min(w[2], rect.x1) - max(w[0], rect.x0) > 0.5 * min(w[2] - w[0], rect.width)]
    if col:
        nearest_y = max(w[3] for w in col)
        row = sorted((w for w in col if abs(w[3] - nearest_y) < 2), key=lambda w: w[0])
        return " ".join(w[4] for w in row[:5]).strip().rstrip(":")
    return ""


def _text_inside(rect: fitz.Rect, words: list[tuple]) -> bool:
    for w in words:
        if not w[4].strip("_"):
            continue
        if _overlap_ratio(fitz.Rect(w[:4]), rect) > 0.3 and (fitz.Rect(w[:4]) & rect).get_area() > 4:
            return True
    return False


def _segments_from_drawings(page: fitz.Page) -> tuple[list, list]:
    """(horizontal, vertical) stroke segments from vector drawings.

    horizontal: (x0, x1, y); vertical: (y0, y1, x).  Thin filled rects count.
    """
    hs, vs = [], []
    for d in page.get_drawings():
        for it in d.get("items", []):
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1:
                    hs.append((min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2))
                elif abs(p1.x - p2.x) < 1:
                    vs.append((min(p1.y, p2.y), max(p1.y, p2.y), (p1.x + p2.x) / 2))
            elif it[0] == "re":
                r = fitz.Rect(it[1]).normalize()
                if r.height <= 2 and r.width > 2:
                    hs.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
                elif r.width <= 2 and r.height > 2:
                    vs.append((r.y0, r.y1, (r.x0 + r.x1) / 2))
    return hs, vs


def _is_grid_rule(seg: tuple, vs: list, tol: float = 1.5) -> bool:
    """A horizontal rule touched by >= 2 vertical rules is a table grid line,
    not a fill-in line."""
    x0, x1, y = seg
    touching = 0
    for vy0, vy1, vx in vs:
        if x0 - tol <= vx <= x1 + tol and vy0 - tol <= y <= vy1 + tol:
            touching += 1
            if touching >= 2:
                return True
    return False


def _table_header_rects(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    """(table bboxes, header-row bboxes) — header rows never become fields."""
    tables, headers = [], []
    try:
        found = page.find_tables().tables
    except Exception:
        found = []
    for t in found:
        tables.append(fitz.Rect(t.bbox))
        hdr = None
        try:
            if t.header is not None and t.header.bbox and not t.header.external:
                hdr = fitz.Rect(t.header.bbox)
        except Exception:
            hdr = None
        if hdr is None:
            try:
                hdr = fitz.Rect(t.rows[0].bbox)
            except Exception:
                hdr = None
        if hdr is not None and not hdr.is_empty:
            headers.append(hdr)
    return tables, headers


# ── raster (scanned page) line / box detection ──


def _runs_1d(mask_row, min_len: int) -> list[tuple[int, int]]:
    import numpy as np

    padded = np.concatenate(([0], mask_row.astype(np.int8), [0]))
    d = np.diff(padded)
    starts = np.nonzero(d == 1)[0]
    ends = np.nonzero(d == -1)[0]
    return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s >= min_len]


def _raster_segments(dark, min_len: int, max_thick: int) -> list[tuple[int, int, int, int]]:
    """Horizontal strokes in a boolean image -> (x0, x1, y0, y1) pixel boxes.

    Runs on consecutive rows with (nearly) the same extent merge into one
    stroke; strokes thicker than ``max_thick`` are solid areas, not lines.
    """
    open_: list[list[int]] = []  # [x0, x1, y0, y1]
    done: list[list[int]] = []
    for y in range(dark.shape[0]):
        runs = _runs_1d(dark[y], min_len)
        nxt = []
        used = set()
        for s in open_:
            hit = None
            for i, (a, b) in enumerate(runs):
                if i not in used and abs(a - s[0]) <= 2 and abs(b - s[1]) <= 2:
                    hit = i
                    break
            if hit is None:
                done.append(s)
            else:
                used.add(hit)
                a, b = runs[hit]
                s[0], s[1], s[3] = min(s[0], a), max(s[1], b), y + 1
                nxt.append(s)
        for i, (a, b) in enumerate(runs):
            if i not in used:
                nxt.append([a, b, y, y + 1])
        open_ = nxt
    done.extend(open_)
    return [tuple(s) for s in done if s[3] - s[2] <= max_thick]


def _scanned_candidates(page: fitz.Page) -> Optional[tuple[list[dict], list[tuple]]]:
    """For a scanned (image-only) page: OCR words for labels plus line / box
    detection on the rendered page.  Returns None for normal pages."""
    try:
        from backend.features import convert as _conv
    except Exception:  # pragma: no cover
        return None
    info = _conv._page_scan_info(page)
    if not info["is_scanned"] or len(page.get_text("words")) > 5:
        return None
    import numpy as np

    words: list[tuple] = []
    langs = _conv._available_languages()
    if langs:
        lang = "eng" if "eng" in langs else langs[0]
        try:
            words = list(_conv._ocr_words(page, lang, 300, _conv._tessdata(lang)))
        except Exception:
            words = []
    dpi = 150
    scale = dpi / 72
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    dark = arr < 140
    max_thick = max(2, int(round(2.5 * scale)))
    hsegs = _raster_segments(dark, int(6 * scale), max_thick)
    vsegs = [(y0, y1, x0, x1) for (y0, y1, x0, x1) in _raster_segments(dark.T, int(6 * scale), max_thick)]
    to_pt = lambda v: v / scale
    H = [(to_pt(a), to_pt(b), to_pt((c + d) / 2)) for a, b, c, d in hsegs]   # (x0, x1, y)
    V = [(to_pt(a), to_pt(b), to_pt((c + d) / 2)) for a, b, c, d in vsegs]   # (y0, y1, x)

    cands: list[dict] = []
    used_h: set[int] = set()
    tol = 3.0
    # Boxes: two horizontal strokes with the same extent joined by verticals at both ends.
    for i, (ax0, ax1, ay) in enumerate(H):
        for j, (bx0, bx1, by) in enumerate(H):
            if j == i or by <= ay + 5 or abs(ax0 - bx0) > tol or abs(ax1 - bx1) > tol:
                continue
            hgt = by - ay
            if hgt > 160:
                continue
            left = any(abs(vx - ax0) <= tol and vy0 <= ay + tol and vy1 >= by - tol for vy0, vy1, vx in V)
            right = any(abs(vx - ax1) <= tol and vy0 <= ay + tol and vy1 >= by - tol for vy0, vy1, vx in V)
            if not (left and right):
                continue
            r = fitz.Rect(ax0, ay, ax1, by)
            w = r.width
            if 6 <= w <= 24 and 6 <= hgt <= 24 and abs(w - hgt) <= 4:
                # tesseract often reads an empty box as "[]", "L]", "O": ignore those
                inner_words = [x for x in words if _overlap_ratio(fitz.Rect(x[:4]), r) > 0.3
                               and sum(ch.isalnum() for ch in x[4]) >= 2]
                if not inner_words:
                    cands.append({"type": "checkbox", "rect": r, "source": "scan-box"})
                    used_h.update((i, j))
            elif w >= 30 and hgt >= 10:
                inner = fitz.Rect(r.x0 + 1.5, r.y0 + 1.5, r.x1 - 1.5, r.y1 - 1.5)
                if not _text_inside(inner, words):
                    cands.append({"type": "text", "rect": inner, "source": "scan-box", "multiline": hgt > 40})
                    used_h.update((i, j))
    # Underlines: long horizontal strokes that are not part of a box or a grid.
    pr = page.rect
    for i, (x0, x1, y) in enumerate(H):
        if i in used_h or x1 - x0 < 50 or x1 - x0 > pr.width * 0.9:
            continue
        if _is_grid_rule((x0, x1, y), V, tol=tol):
            continue
        fr = fitz.Rect(x0, y - 16, x1, y - 1.5)
        if _text_inside(fr, words):
            continue
        cands.append({"type": "text", "rect": fr, "source": "scan-line"})
    # "Label:" followed by blank space, from OCR lines.
    lines: dict[tuple, list[tuple]] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)
    for ws in lines.values():
        ws.sort(key=lambda w: w[0])
        for k, w in enumerate(ws):
            if not w[4].endswith(":"):
                continue
            lb = fitz.Rect(ws[0][:4])
            for x in ws[: k + 1]:
                lb |= fitz.Rect(x[:4])
            nxt = ws[k + 1][0] if k + 1 < len(ws) else pr.x1 - 36
            start, limit = lb.x1 + 4, nxt - 6
            if limit - start < 60:
                continue
            # A heading such as "Comments:" or "Symptoms (check all that apply):"
            # whose answer is a box or a column of option boxes directly BELOW
            # it: the blank space to its right is not a field.
            if any(c["source"] == "scan-box" and 0 <= c["rect"].y0 - lb.y1 <= 30
                   and lb.x0 - 20 <= c["rect"].x0 <= lb.x0 + 120 for c in cands):
                continue
            cy = (lb.y0 + lb.y1) / 2
            h = max(14.0, lb.height + 4)
            cands.append({"type": "text", "source": "scan-label", "rect": fitz.Rect(start, cy - h / 2, limit, cy + h / 2)})
    # Label words: drop OCR noise (tokens without letters/digits, e.g. "|" for a
    # box edge) and whatever tesseract read off the checkboxes themselves.
    boxes = [c["rect"] for c in cands if c["type"] == "checkbox"]
    clean = []
    for w in words:
        if not any(ch.isalnum() for ch in w[4]):
            continue
        if any(_overlap_ratio(fitz.Rect(w[:4]), b) > 0.5 for b in boxes):
            continue
        clean.append(w)
    return cands, clean


_DATE_LABEL_RE = re.compile(r"(\bdate\b|\bdob\b|d\.o\.b|\bbirth|\bbirthday\b|\bdated\b)", re.I)
_MULTI_HINT_RE = re.compile(r"(all that apply|check all|select all|any that apply)", re.I)


def _row_text_left(r: fitz.Rect, words: list[tuple], exclude: list[fitz.Rect]) -> str:
    cy = (r.y0 + r.y1) / 2
    left = [w for w in words if w[2] <= r.x0 + 1 and w[1] - 2 <= cy <= w[3] + 2 and r.x0 - w[2] < 300
            and w[4].strip("_") and not any(fitz.Rect(w[:4]).intersects(e) for e in exclude)]
    left.sort(key=lambda w: w[0])
    if not left:
        return ""
    run = [left[-1]]
    for w in reversed(left[:-1]):
        if run[0][0] - w[2] < 14:
            run.insert(0, w)
        else:
            break
    return " ".join(w[4] for w in run).strip()


def _text_above(r: fitz.Rect, words: list[tuple]) -> str:
    above = [w for w in words if 0 <= r.y0 - w[3] < 24 and r.x0 - 40 <= w[0] <= r.x0 + 250 and w[4].strip("_")]
    if not above:
        return ""
    y = max(w[3] for w in above)
    row = sorted((w for w in above if abs(w[3] - y) < 3), key=lambda w: w[0])
    return " ".join(w[4] for w in row).strip()


def _group_radios(cands: list[dict], words: list[tuple]) -> None:
    """Turn rows/columns of option boxes that share a question label into radio groups.

    Row:    "Gender:  [ ] Male  [ ] Female  [ ] Other"
    Column: "Preferred contact?" with option boxes stacked under it.
    Boxes whose question says "check all that apply" stay checkboxes.
    """
    boxes = [c for c in cands if c["type"] == "checkbox"]
    taken: set[int] = set()

    def finish(group: list[dict], question: str):
        q = question.strip().rstrip(":?").strip()
        if not q or _MULTI_HINT_RE.search(question):
            return
        exports: list[str] = []
        for n, c in enumerate(group):
            e = (c.get("label") or "").strip() or f"Choice{n + 1}"
            while e in exports:
                e += "_"
            exports.append(e)
        for c, e in zip(group, exports):
            c["type"] = "radio"
            c["group"] = q
            c["export"] = e
            taken.add(id(c))

    # rows
    rows: list[list[dict]] = []
    for c in sorted(boxes, key=lambda c: (c["rect"].y0, c["rect"].x0)):
        cy = (c["rect"].y0 + c["rect"].y1) / 2
        for row in rows:
            r0 = row[0]["rect"]
            if abs(cy - (r0.y0 + r0.y1) / 2) <= max(3.0, r0.height * 0.4) and abs(c["rect"].height - r0.height) <= 4:
                row.append(c)
                break
        else:
            rows.append([c])
    for row in rows:
        if len(row) < 2:
            continue
        row.sort(key=lambda c: c["rect"].x0)
        opt_rects = [fitz.Rect(c["rect"]) for c in row]
        # option labels sit right of each box; the question is left of the first box
        question = _row_text_left(row[0]["rect"], words, opt_rects)
        if question:
            finish(row, question)
    # columns
    cols: list[list[dict]] = []
    for c in sorted((b for b in boxes if id(b) not in taken), key=lambda c: (c["rect"].x0, c["rect"].y0)):
        for col in cols:
            last = col[-1]["rect"]
            if abs(c["rect"].x0 - last.x0) <= 3 and 0 < c["rect"].y0 - last.y1 <= max(14.0, last.height * 2.5):
                col.append(c)
                break
        else:
            cols.append([c])
    for col in cols:
        if len(col) < 2:
            continue
        question = _text_above(col[0]["rect"], words)
        if question and question.rstrip().endswith((":", "?")):
            finish(col, question)


def _detect_on_page(page: fitz.Page, taken_rects: list[fitz.Rect]) -> list[dict]:
    """Return candidate fields in *unrotated* page coordinates."""
    scanned = _scanned_candidates(page)
    if scanned is not None:
        cands, words = scanned
        headers: list[fitz.Rect] = []
    else:
        cands, words, headers = _vector_candidates(page)
    pr = page.rect

    # De-duplicate (priority = order above) and against existing widgets.
    accepted: list[dict] = []
    for c in cands:
        r = c["rect"]
        if r.width < 4 or r.height < 4 or not r.intersects(pr):
            continue
        # Table header rows are column titles, never fill-in fields.
        if any(_overlap_ratio(r, h) > 0.2 for h in headers):
            continue
        if any(_overlap_ratio(r, t) > 0.25 for t in taken_rects):
            continue
        if any(_overlap_ratio(r, a["rect"]) > 0.25 for a in accepted):
            continue
        c["label"] = _label_for(r, words, prefer_right=c["type"] == "checkbox")
        if c["type"] == "text" and _DATE_LABEL_RE.search(c["label"] or ""):
            c["format"] = "date"
        accepted.append(c)
    _group_radios(accepted, words)
    return accepted


def _vector_candidates(page: fitz.Page) -> tuple[list[dict], list[tuple], list[fitz.Rect]]:
    words = page.get_text("words")
    pr = page.rect
    cands: list[dict] = []
    hs, vs = _segments_from_drawings(page)
    _tables, headers = _table_header_rects(page)

    # 1. Rectangles from vector drawings: small squares -> checkboxes, empty boxes -> text fields.
    rects: list[fitz.Rect] = []
    for d in page.get_drawings():
        fill = d.get("fill")
        stroke = d.get("color")
        dark_fill = fill is not None and sum(fill) / max(1, len(fill)) < 0.85
        if dark_fill and stroke is None:
            continue  # solid shapes / shading, not an input box
        for it in d.get("items", []):
            if it[0] == "re":
                rects.append(fitz.Rect(it[1]))
            elif it[0] == "qu":
                rects.append(it[1].rect)
    # unique, and drop boxes that contain other boxes (outer frames / table borders)
    uniq: list[fitz.Rect] = []
    for r in rects:
        r = fitz.Rect(r).normalize()
        if not any(abs(r.x0 - u.x0) < 1 and abs(r.y0 - u.y0) < 1 and abs(r.x1 - u.x1) < 1 and abs(r.y1 - u.y1) < 1 for u in uniq):
            uniq.append(r)
    for r in uniq:
        w, h = r.width, r.height
        if 6 <= w <= 24 and 6 <= h <= 24 and abs(w - h) <= 3:
            if not _text_inside(r, words):
                cands.append({"type": "checkbox", "rect": fitz.Rect(r), "source": "box"})
            continue
        if w >= 30 and 10 <= h <= 160 and w < pr.width * 0.95:
            if any(o != r and r.contains(o) and o.width > 4 for o in uniq):
                continue
            if _text_inside(r, words):
                continue
            inner = fitz.Rect(r.x0 + 1.5, r.y0 + 1.5, r.x1 - 1.5, r.y1 - 1.5)
            cands.append({"type": "text", "rect": inner, "source": "box", "multiline": h > 40})

    # 2. Checkbox glyphs in text (☐ etc.)
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    if ch["c"] in _BOX_GLYPHS:
                        b = fitz.Rect(ch["bbox"])
                        side = min(b.width, b.height)
                        cy = (b.y0 + b.y1) / 2
                        cx = (b.x0 + b.x1) / 2
                        cands.append({"type": "checkbox", "source": "glyph",
                                      "rect": fitz.Rect(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)})

    # 3. Underscore runs "_____"
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            run: list[fitz.Rect] = []
            chars = [c for s in line.get("spans", []) for c in s.get("chars", [])]
            for c in chars + [{"c": "\0", "bbox": (0, 0, 0, 0)}]:
                if c["c"] == "_":
                    run.append(fitz.Rect(c["bbox"]))
                    continue
                if len(run) >= 3:
                    u = fitz.Rect(run[0])
                    for rr in run[1:]:
                        u |= rr
                    if u.width >= 20:
                        hgt = max(12.0, u.height)
                        cands.append({"type": "text", "source": "underscore",
                                      "rect": fitz.Rect(u.x0, u.y1 - hgt, u.x1, u.y1)})
                run = []

    # 4. Horizontal rule lines (signature/fill lines) not part of a box.
    for d in page.get_drawings():
        for it in d.get("items", []):
            seg = None
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1 and abs(p1.x - p2.x) >= 50:
                    seg = (min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2)
            elif it[0] == "re":
                r = fitz.Rect(it[1])
                if r.height <= 2 and r.width >= 50:
                    seg = (r.x0, r.x1, (r.y0 + r.y1) / 2)
            if not seg:
                continue
            x0, x1, y = seg
            if x1 - x0 > pr.width * 0.9:
                continue  # page-wide separators
            if _is_grid_rule(seg, vs):
                continue  # table grid rule (e.g. the line above a header row)
            # part of a drawn box? (an existing rect shares this edge)
            if any(abs(u.y0 - y) < 1.5 or abs(u.y1 - y) < 1.5 for u in uniq
                   if u.height > 3 and u.x0 - 1 <= x0 and u.x1 + 1 >= x1):
                continue
            fr = fitz.Rect(x0, y - 16, x1, y - 1)
            if _text_inside(fr, words):
                continue  # underline under existing text
            cands.append({"type": "text", "source": "line", "rect": fr})

    # 5. "Label:" followed by blank space on the same row.
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            txt = "".join(s["text"] for s in line.get("spans", [])).rstrip()
            if txt:
                lines.append((fitz.Rect(line["bbox"]), txt))
    for lb, txt in lines:
        if not txt.endswith(":") or len(txt) > 60:
            continue
        cy = (lb.y0 + lb.y1) / 2
        right_words = [w for w in words if w[0] > lb.x1 + 1 and w[1] - 2 <= cy <= w[3] + 2]
        limit = min([w[0] for w in right_words], default=pr.x1 - 36) - 6
        start = lb.x1 + 4
        if limit - start < 60:
            continue
        h = max(14.0, lb.height + 4)
        fr = fitz.Rect(start, cy - h / 2, limit, cy + h / 2)
        cands.append({"type": "text", "source": "label", "rect": fr})

    return cands, words, headers


class DetectRequest(BaseModel):
    pages: Optional[list[int]] = None  # default: all pages
    dry_run: bool = False
    types: list[str] = Field(default_factory=lambda: ["text", "checkbox", "radio"])


def _detect(doc: fitz.Document, req: DetectRequest) -> dict:
    pages = req.pages if req.pages is not None else list(range(doc.page_count))
    taken_names = _existing_names(doc)
    created: list[dict] = []
    candidates: list[dict] = []
    for pno in pages:
        page = _check_page(doc, pno)
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        try:
            taken = [fitz.Rect(w.rect) for w in page.widgets()]
            found = _detect_on_page(page, taken)
            if "radio" not in req.types:
                for c in found:
                    if c["type"] == "radio":
                        c["type"] = "checkbox"
            found = [c for c in found if c["type"] in req.types]
            # name assignment (one name per radio group)
            group_names: dict[str, str] = {}
            for c in found:
                if c["type"] == "radio":
                    g = c["group"]
                    if g not in group_names:
                        group_names[g] = _unique_name(g, taken_names)
                        taken_names.add(group_names[g])
                    c["name"] = group_names[g]
                    continue
                base = c["label"] or ("checkbox" if c["type"] == "checkbox" else "text")
                c["name"] = _unique_name(base, taken_names)
                taken_names.add(c["name"])
            if not req.dry_run:
                for c in found:
                    if c["type"] == "radio":
                        c["id"] = _add_radio_kid(doc, page, c["rect"], c["name"], c["export"],
                                                 FF_RADIO | FF_NOTOGGLETOOFF, c["group"])
                        continue
                    w = fitz.Widget()
                    w.field_type = TYPE_CODES[c["type"]]
                    w.field_name = c["name"]
                    w.rect = c["rect"]
                    if c["label"]:
                        w.field_label = c["label"]
                    if c["type"] == "text":
                        w.text_fontsize = 0
                        w.text_font = "Helv"
                        if c.get("multiline"):
                            w.field_flags = FF_MULTILINE
                    else:
                        w.field_value = False
                    annot = page.add_widget(w)
                    c["id"] = annot.xref
                    if c.get("format") == "date":
                        _set_date_format(doc, annot.xref)
                        if not c["label"]:
                            doc.xref_set_key(annot.xref, "TU", _pdf_str(f"Date ({DATE_FORMAT})"))
        finally:
            if rot:
                page.set_rotation(rot)
        for c in found:
            out = {
                "page": pno,
                "type": c["type"],
                "name": c["name"],
                "label": c["label"],
                "source": c["source"],
                "rect": _display_rect(page, c["rect"]),
            }
            if "id" in c:
                out["id"] = c["id"]
            if c["type"] == "radio":
                out["export_value"] = c["export"]
                out["group"] = c["group"]
            if c.get("format"):
                out["format"] = c["format"]
            (candidates if req.dry_run else created).append(out)
    return {"created": created, "candidates": candidates, "count": len(created) or len(candidates)}


# ─── Export / import ─────────────────────────────────────────────────────────


def _fdf_unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            mapping = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "(": "(", ")": ")", "\\": "\\"}
            if n in mapping:
                out.append(mapping[n])
                i += 2
                continue
            m = re.match(r"[0-7]{1,3}", s[i + 1:])
            if m:
                out.append(chr(int(m.group(0), 8)))
                i += 1 + len(m.group(0))
                continue
            i += 1
            continue
        out.append(c)
        i += 1
    txt = "".join(out)
    if txt.startswith("\xfe\xff"):
        txt = txt[2:].encode("latin-1").decode("utf-16-be")
    return txt


def _parse_pdf_string_at(s: str, i: int) -> tuple[str, int]:
    """Parse a (literal) or <hex> string starting at s[i]; return (text, end)."""
    if s[i] == "<":
        j = s.index(">", i)
        raw = bytes.fromhex(re.sub(r"\s", "", s[i + 1:j]))
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be"), j + 1
        return raw.decode("latin-1"), j + 1
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return _fdf_unescape(s[i + 1:j]), j + 1
        j += 1
    raise ValueError("unterminated string")


def _parse_fdf(text: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for m in re.finditer(r"/T\s*([(<])", text):
        try:
            name, end = _parse_pdf_string_at(text, m.start(1))
        except ValueError:
            continue
        # look for /V within the same field dict (before the next /T)
        nxt = text.find("/T", end)
        seg = text[end: nxt if nxt != -1 else len(text)]
        vm = re.search(r"/V\s*", seg)
        if not vm:
            # /V may appear before /T in the dict
            prev_seg = text[max(0, m.start() - 400): m.start()]
            vm2 = list(re.finditer(r"<<[^<>]*?/V\s*(/[^\s/>\]]+|\(|<(?!<))", prev_seg))
            if not vm2:
                continue
            seg = prev_seg[vm2[-1].start():]
            vm = re.search(r"/V\s*", seg)
        k = vm.end()
        if k >= len(seg):
            continue
        if seg[k] == "/":
            nm = re.match(r"/([^\s/>\]\[()<]+)", seg[k:])
            raw = nm.group(1) if nm else ""
            values[name] = re.sub(r"#([0-9A-Fa-f]{2})", lambda x: chr(int(x.group(1), 16)), raw)
        elif seg[k] in "(<":
            values[name], _ = _parse_pdf_string_at(seg, k)
        elif seg[k] == "[":
            arr = seg[k + 1: seg.index("]", k)]
            items, p = [], 0
            while p < len(arr):
                if arr[p] in "(<":
                    t, p = _parse_pdf_string_at(arr, p)
                    items.append(t)
                else:
                    p += 1
            values[name] = items
    return values


def _parse_xfdf(text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        raise HTTPException(status_code=400, detail=f"Invalid XFDF: {e}")
    values: dict[str, Any] = {}

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def walk(el, prefix: str):
        for child in el:
            if local(child.tag) != "field":
                continue
            nm = child.get("name", "")
            full = f"{prefix}.{nm}" if prefix else nm
            vals = [v.text or "" for v in child if local(v.tag) == "value"]
            if vals:
                values[full] = vals[0] if len(vals) == 1 else vals
            walk(child, full)

    for fields in root.iter():
        if local(fields.tag) == "fields":
            walk(fields, "")
            break
    return values


def _export_value_for_fdf(f_type: str, v: Any, export: Optional[str]) -> Optional[str]:
    if f_type == "checkbox":
        if isinstance(v, str):
            return "/" + _pdf_name(v)
        return "/" + (_pdf_name(export or "Yes") if v else "Off")
    if f_type == "radio":
        return "/" + (_pdf_name(v) if v else "Off")
    if isinstance(v, list):
        return "[" + " ".join(_pdf_str(str(x)) for x in v) + "]"
    return _pdf_str("" if v is None else str(v))


def _build_fdf(fields: list[dict], values: dict[str, Any]) -> str:
    types = {}
    exports = {}
    for f in fields:
        types.setdefault(f["name"], f["type"])
        if f["type"] == "checkbox":
            exports.setdefault(f["name"], f["export_value"])
    entries = []
    for name, v in values.items():
        entries.append(f"<< /T {_pdf_str(name)} /V {_export_value_for_fdf(types[name], v, exports.get(name))} >>")
    body = "\n".join(entries)
    return (
        "%FDF-1.2\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /FDF << /Fields [\n" + body + "\n] >> >>\nendobj\n"
        "trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def _build_xfdf(fields: list[dict], values: dict[str, Any]) -> str:
    root = ET.Element("xfdf", {"xmlns": "http://ns.adobe.com/xfdf/", "xml:space": "preserve"})
    fe = ET.SubElement(root, "fields")
    types = {f["name"]: f["type"] for f in fields}
    exports = {f["name"]: f["export_value"] for f in fields if f["type"] == "checkbox"}
    for name, v in values.items():
        el = ET.SubElement(fe, "field", {"name": name})
        if types.get(name) == "checkbox" and isinstance(v, bool):
            v = (exports.get(name) or "Yes") if v else "Off"
        if types.get(name) == "radio" and not v:
            v = "Off"
        for item in (v if isinstance(v, list) else [v]):
            ET.SubElement(el, "value").text = "" if item is None else str(item)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# ─── Routes ──────────────────────────────────────────────────────────────────


@router.get("/{doc_id}/form-fields")
async def list_form_fields(doc_id: str, page: Optional[int] = None):
    """List every widget with rect (display points), value, options and flags."""
    with _open_doc(doc_id) as doc:
        if page is not None:
            _check_page(doc, page)
        fields = _list_fields(doc, page)
        pages = [
            {"index": i, "width": round(doc[i].rect.width, 2), "height": round(doc[i].rect.height, 2),
             "rotation": doc[i].rotation}
            for i in range(doc.page_count)
        ]
        return {"fields": fields, "count": len(fields), "is_form": bool(doc.is_form_pdf), "pages": pages}


class FillRequest(BaseModel):
    values: dict[str, Union[bool, str, list[str], None]]


@router.post("/{doc_id}/form-fields/fill")
async def fill_form_fields(doc_id: str, req: FillRequest):
    """Fill several fields by name.  Checkboxes take true/false (or their export
    value); radio groups take the export value of the button to select (or
    null to clear); combo/list take an option value or label."""
    if not req.values:
        raise HTTPException(status_code=400, detail="values is empty")
    with _open_doc(doc_id, "Fill form") as doc:
        filled, errors = _fill_values(doc, dict(req.values))
        if not filled:
            raise HTTPException(status_code=400, detail={"message": "No fields were filled", "errors": errors})
    return {"status": "ok", "filled": filled, "errors": errors}


@router.post("/{doc_id}/form-fields")
async def create_form_field(doc_id: str, req: CreateFieldRequest):
    with _open_doc(doc_id, f"Add {req.type} field") as doc:
        xref = _create_field(doc, req)
        page, w = _find_widget(doc, xref)
        info = [f for f in _list_fields(doc, page.number) if f["id"] == xref][0]
    return {"status": "ok", "field": info}


@router.patch("/{doc_id}/form-fields/{field_id}")
async def update_form_field(doc_id: str, field_id: int, req: UpdateFieldRequest):
    with _open_doc(doc_id, "Edit form field") as doc:
        _update_field(doc, field_id, req)
        page, w = _find_widget(doc, field_id)
        info = [f for f in _list_fields(doc, page.number) if f["id"] == field_id][0]
    return {"status": "ok", "field": info}


@router.delete("/{doc_id}/form-fields/{field_id}")
async def delete_form_field(doc_id: str, field_id: int, whole_field: bool = Query(False)):
    with _open_doc(doc_id, "Delete form field") as doc:
        page, w = _find_widget(doc, field_id)
        name = w.field_name
        targets = [field_id]
        if whole_field and name:
            targets = [ww.xref for _pg, ww in _iter_widgets(doc) if ww.field_name == name]
        deleted = []
        for xref in targets:
            pg, ww = _find_widget(doc, xref)
            _delete_widget(doc, pg, ww)
            deleted.append(xref)
    return {"status": "ok", "deleted": deleted, "name": name}


@router.post("/{doc_id}/form-fields/flatten")
async def flatten_form_fields(doc_id: str):
    """Bake current field appearances into page content and remove all widgets."""
    with _open_doc(doc_id, "Flatten form") as doc:
        count = 0
        for _pg, w in list(_iter_widgets(doc)):
            count += 1
            ftype = TYPE_NAMES.get(w.field_type)
            # Regenerate appearance streams for value-bearing fields so that
            # files relying on /NeedAppearances still flatten with their values.
            if ftype in ("text", "combo", "list") and w.field_value:
                try:
                    w.update()
                except Exception:
                    pass
        if count == 0:
            raise HTTPException(status_code=400, detail="Document has no form fields")
        doc.bake(annots=False, widgets=True)
        cat = doc.pdf_catalog()
        if doc.xref_get_key(cat, "AcroForm")[0] != "null":
            doc.xref_set_key(cat, "AcroForm", "null")
    return {"status": "ok", "flattened": count}


@router.post("/{doc_id}/form-fields/detect")
async def detect_form_fields(doc_id: str, req: Optional[DetectRequest] = None):
    """Find fill-in areas on flat forms (underscores, empty boxes, rule lines,
    'Label:' + blank space, ☐ glyphs) and create text fields / checkboxes.

    Table header rows never become fields.  Option boxes sharing a question
    label become radio groups; blanks labelled Date/DOB get Acrobat date
    format actions.  Scanned (image-only) pages are OCR'd for labels and the
    rendered page is searched for underline / box strokes."""
    req = req or DetectRequest()
    bad = [t for t in req.types if t not in ("text", "checkbox", "radio")]
    if bad:
        raise HTTPException(status_code=400, detail=f"unsupported detect types {bad}")
    if req.dry_run:
        with _open_doc(doc_id) as doc:
            return _detect(doc, req)
    # Only snapshot/save if something was actually created.
    with _open_doc(doc_id) as doc:
        preview = _detect(doc, DetectRequest(pages=req.pages, dry_run=True, types=req.types))
    if not preview["candidates"]:
        return {"created": [], "candidates": [], "count": 0}
    with _open_doc(doc_id, "Auto-detect form fields") as doc:
        return _detect(doc, req)


@router.get("/{doc_id}/form-fields/export")
async def export_form_data(doc_id: str, format: str = "json"):
    fmt = format.lower()
    if fmt not in ("json", "fdf", "xfdf"):
        raise HTTPException(status_code=400, detail="format must be json, fdf or xfdf")
    with _open_doc(doc_id) as doc:
        fields = _list_fields(doc)
        values = _values_by_name(fields)
    if fmt == "json":
        body = json.dumps({"format": "pdf-form-data/v1", "fields": values}, indent=2, ensure_ascii=False)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="form-data.json"'})
    if fmt == "fdf":
        body = _build_fdf(fields, values)
        return Response(body.encode("latin-1", errors="replace"), media_type="application/vnd.fdf",
                        headers={"Content-Disposition": 'attachment; filename="form-data.fdf"'})
    body = _build_xfdf(fields, values)
    return Response(body.encode("utf-8"), media_type="application/vnd.adobe.xfdf",
                    headers={"Content-Disposition": 'attachment; filename="form-data.xfdf"'})


class ImportRequest(BaseModel):
    format: str = "json"  # json | fdf | xfdf
    data: Union[str, dict]


@router.post("/{doc_id}/form-fields/import")
async def import_form_data(doc_id: str, req: ImportRequest):
    fmt = req.format.lower()
    if fmt == "json":
        payload = req.data
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON must be an object")
        values = payload.get("fields", payload)
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="'fields' must be an object")
    elif fmt == "fdf":
        if not isinstance(req.data, str):
            raise HTTPException(status_code=400, detail="FDF data must be a string")
        values = _parse_fdf(req.data)
    elif fmt == "xfdf":
        if not isinstance(req.data, str):
            raise HTTPException(status_code=400, detail="XFDF data must be a string")
        values = _parse_xfdf(req.data)
    else:
        raise HTTPException(status_code=400, detail="format must be json, fdf or xfdf")
    if not values:
        raise HTTPException(status_code=400, detail="No field values found in import data")
    # FDF/XFDF use "Off" for unchecked boxes / unselected radios.
    with _open_doc(doc_id, "Import form data") as doc:
        types = {f["name"]: f["type"] for f in _list_fields(doc)}
        norm = {}
        for k, v in values.items():
            if types.get(k) == "radio" and v == "Off":
                v = None
            norm[k] = v
        filled, errors = _fill_values(doc, norm)
        if not filled:
            raise HTTPException(status_code=400, detail={"message": "No fields matched", "errors": errors})
    return {"status": "ok", "filled": filled, "errors": errors}
