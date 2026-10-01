"""Platform tests: signed-document guard (main.py middleware), .env loading,
redaction history purge and the single UPLOAD_DIR.

These run against the REAL app from backend.main so the middleware stack and
every mounted router are exercised exactly as in production.
"""

import io
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import fitz
import pytest
from httpx import AsyncClient

from backend import advanced_ops
from backend import main as main_mod

REPO = Path(__file__).resolve().parents[2]
SECRET = "123-45-6789"
SIG_HEADER = {"X-Allow-Break-Signature": "1"}
_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


# ─── helpers ─────────────────────────────────────────────────────────────────


def _plain_pdf(text_lines=("Agreement page 1", "Agreement page 2")) -> bytes:
    doc = fitz.open()
    for t in text_lines:
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), t, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


async def _upload(client: AsyncClient, data: bytes) -> str:
    r = await client.post("/api/pdf/upload", files={"file": ("t.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _pdf_path(doc_id: str) -> Path:
    return main_mod.UPLOAD_DIR / doc_id / "original.pdf"


async def _sign(client: AsyncClient, doc_id: str, name: str, rect) -> dict:
    r = await client.post("/api/pdf/signing/certificates", json={"name": name, "email": "x@example.com"})
    assert r.status_code == 200, r.text
    cert = r.json()
    r = await client.post(f"/api/pdf/{doc_id}/sign/digital",
                          json={"cert_id": cert["cert_id"], "page": 0, "rect": rect})
    assert r.status_code == 200, r.text
    return cert


@pytest.fixture
async def signed_doc(client):
    doc_id = await _upload(client, _plain_pdf())
    cert = await _sign(client, doc_id, "Alice Signer", [72, 600, 272, 680])
    yield doc_id
    await client.delete(f"/api/pdf/{doc_id}")
    await client.delete(f"/api/pdf/signing/certificates/{cert['cert_id']}")


def _mutating_doc_routes():
    """Every (method, template) on the real app that takes a {doc_id}."""
    out = []
    for r in main_mod.app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "{doc_id}" not in path:
            continue
        for m in sorted(methods & set(main_mod.GUARDED_METHODS)):
            out.append((m, path))
    return out


def _fill(template: str, doc_id: str) -> str:
    def sub(m):
        return doc_id if m.group(1) == "doc_id" else "0"
    return _PARAM_RE.sub(sub, template)


# ─── signed-document guard ───────────────────────────────────────────────────


async def test_signature_detected_with_signer_name(client, signed_doc):
    info = advanced_ops.signature_info(_pdf_path(signed_doc))
    assert info["signed"] is True and info["count"] == 1
    assert info["signers"] == ["Alice Signer"]


async def test_every_mutating_route_is_classified_and_guarded(client, signed_doc):
    """Enumerates app.routes: any mutating route not explicitly allowed or
    sign-exempt MUST answer 409 on a signed doc and leave the file untouched.
    New routes are guarded by default, so they cannot slip through."""
    routes = _mutating_doc_routes()
    assert len(routes) > 60, routes  # sanity: we really walked every router
    pdf = _pdf_path(signed_doc)
    before = pdf.read_bytes()
    guarded, exempt, allowed = [], [], []
    for method, template in routes:
        cls = main_mod.signed_guard_classify(method, template)
        if cls == "read":
            allowed.append((method, template))
            continue
        if cls == "exempt":
            exempt.append((method, template))
            continue
        guarded.append((method, template))
        r = await client.request(method, _fill(template, signed_doc), json={})
        assert r.status_code == 409, (method, template, r.status_code, r.text[:200])
        body = r.json()
        assert body["code"] == "signed_document"
        assert "Alice Signer" in body["detail"]
        assert pdf.read_bytes() == before, f"{method} {template} modified a signed PDF"

    # Explicit allow-list must not contain stale entries, and every allowed
    # route is one of the reviewed ones (not a silent default).
    live = set(routes)
    for entry in main_mod.SIGNED_GUARD_ALLOW:
        assert entry in live, f"stale allow-list entry {entry}"
    assert set(allowed) == set(main_mod.SIGNED_GUARD_ALLOW)
    assert all(t.startswith("/api/pdf/{doc_id}/sign/") for _, t in exempt)
    # Spot-check the tools the QA round flagged as silently breaking signatures.
    for must in [
        ("POST", "/api/pdf/{doc_id}/text-edit/edit"),
        ("POST", "/api/pdf/{doc_id}/organize/rotate"),
        ("POST", "/api/pdf/{doc_id}/redact/apply"),
        ("POST", "/api/pdf/{doc_id}/form-fields/fill"),
        ("POST", "/api/pdf/{doc_id}/watermark"),
        ("POST", "/api/pdf/{doc_id}/compress"),
        ("PATCH", "/api/pdf/{doc_id}/edit"),
    ]:
        assert must in guarded, must


async def test_header_allows_edit_and_really_rotates(client, signed_doc):
    pdf = _pdf_path(signed_doc)
    r = await client.post(f"/api/pdf/{signed_doc}/organize/rotate",
                          json={"pages": [1], "angle": 90}, headers=SIG_HEADER)
    assert r.status_code == 200, r.text
    doc = fitz.open(str(pdf))
    assert doc[1].rotation == 90
    doc.close()


async def test_reads_still_work_on_signed_doc(client, signed_doc):
    assert (await client.post(f"/api/pdf/{signed_doc}/find", json={"find_text": "Agreement"})).status_code == 200
    assert (await client.get(f"/api/pdf/{signed_doc}/text")).status_code == 200
    assert (await client.get(f"/api/pdf/{signed_doc}/export")).status_code == 200
    v = await client.get(f"/api/pdf/{signed_doc}/sign/validate")
    assert v.status_code == 200 and v.json()["signatures"][0]["intact"]


async def test_second_signature_still_allowed_and_first_stays_valid(client, signed_doc):
    cert = await _sign(client, signed_doc, "Bob Second", [300, 600, 500, 680])
    try:
        sigs = (await client.get(f"/api/pdf/{signed_doc}/sign/validate")).json()["signatures"]
        assert [s["signer_name"] for s in sigs] == ["Alice Signer", "Bob Second"]
        assert all(s["intact"] for s in sigs)
        assert advanced_ops.signature_info(_pdf_path(signed_doc))["count"] == 2
    finally:
        await client.delete(f"/api/pdf/signing/certificates/{cert['cert_id']}")


async def test_unsigned_doc_not_blocked(client, uploaded_doc_id):
    r = await client.post(f"/api/pdf/{uploaded_doc_id}/organize/rotate", json={"pages": [0], "angle": 90})
    assert r.status_code == 200, r.text
    doc = fitz.open(str(_pdf_path(uploaded_doc_id)))
    assert doc[0].rotation == 90
    doc.close()


async def test_cache_invalidates_when_file_changes(client):
    doc_id = await _upload(client, _plain_pdf())
    try:
        assert advanced_ops.signature_info(_pdf_path(doc_id))["signed"] is False
        r = await client.post(f"/api/pdf/{doc_id}/organize/rotate", json={"pages": [0], "angle": 90})
        assert r.status_code == 200  # unsigned → not blocked
        cert = await _sign(client, doc_id, "Carol Later", [72, 600, 272, 680])
        # Same path, new bytes → cache must notice.
        assert advanced_ops.signature_info(_pdf_path(doc_id))["signed"] is True
        r = await client.post(f"/api/pdf/{doc_id}/organize/rotate", json={"pages": [0], "angle": 90})
        assert r.status_code == 409
        await client.delete(f"/api/pdf/signing/certificates/{cert['cert_id']}")
    finally:
        await client.delete(f"/api/pdf/{doc_id}")


async def test_409_carries_cors_headers(client, signed_doc):
    r = await client.post(f"/api/pdf/{signed_doc}/organize/rotate", json={"pages": [0], "angle": 90},
                          headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 409
    assert r.headers.get("access-control-allow-origin")


async def test_chat_edit_on_signed_doc_is_reverted(client, signed_doc, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # deterministic regex path
    main_mod._ai_request_times.clear()
    pdf = _pdf_path(signed_doc)
    before = pdf.read_bytes()
    hist_before = (pdf.parent / "history.json").read_text() if (pdf.parent / "history.json").exists() else None
    r = await client.post(f"/api/pdf/{signed_doc}/chat", json={"message": "rotate page 1", "current_page": 0})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "signed_document"
    assert pdf.read_bytes() == before
    hist_after = (pdf.parent / "history.json").read_text() if (pdf.parent / "history.json").exists() else None
    assert hist_after == hist_before
    # A question is not blocked.
    q = await client.post(f"/api/pdf/{signed_doc}/chat", json={"message": "how many pages?", "current_page": 0})
    assert q.status_code == 200, q.text
    # With consent the edit goes through.
    ok = await client.post(f"/api/pdf/{signed_doc}/chat", json={"message": "rotate page 1", "current_page": 0},
                           headers=SIG_HEADER)
    assert ok.status_code == 200 and ok.json()["changed"] is True
    doc = fitz.open(str(pdf))
    assert doc[0].rotation != 0
    doc.close()


# ─── redaction purge ─────────────────────────────────────────────────────────


def _files_containing(doc_dir: Path, needle: str) -> list[str]:
    """Every file under doc_dir whose content (PDF text or raw bytes) has needle."""
    hits = []
    for f in doc_dir.rglob("*"):
        if not f.is_file():
            continue
        raw = f.read_bytes()
        if needle.encode() in raw:
            hits.append(str(f.relative_to(doc_dir)))
            continue
        if raw[:5] == b"%PDF-":
            try:
                d = fitz.open(stream=raw, filetype="pdf")
                text = "".join(p.get_text() for p in d)
                d.close()
            except Exception:
                continue
            if needle in text:
                hits.append(str(f.relative_to(doc_dir)))
    return hits


async def test_apply_redactions_purges_all_unredacted_copies(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    doc_id = await _upload(client, _plain_pdf((f"Employee SSN {SECRET} on file", "Second page")))
    doc_dir = main_mod.UPLOAD_DIR / doc_id
    try:
        # Build up undo history + derived caches that hold the secret.
        assert (await client.post(f"/api/pdf/{doc_id}/organize/rotate",
                                  json={"pages": [1], "angle": 90})).status_code == 200
        assert (await client.get(f"/api/pdf/{doc_id}/analysis")).status_code == 200
        main_mod._ai_request_times.clear()
        await client.post(f"/api/pdf/{doc_id}/chat", json={"message": f"find {SECRET}", "current_page": 0})
        assert _files_containing(doc_dir, SECRET), "precondition: secret present before redaction"

        found = (await client.post(f"/api/pdf/{doc_id}/redact/search", json={"query": SECRET})).json()
        areas = [{"page": m["page"], "rect": r} for m in found["matches"] for r in m["rects"]]
        assert areas
        r = await client.post(f"/api/pdf/{doc_id}/redact/apply", json={"areas": areas})
        assert r.status_code == 200, r.text
        assert r.json()["undoable"] is False and r.json()["history_purged"] is True

        assert _files_containing(doc_dir, SECRET) == []
        assert not list((doc_dir / "history").iterdir())
        # Not undoable, with an explanation.
        u = await client.post(f"/api/pdf/{doc_id}/undo")
        assert u.status_code == 400 and "redactions were applied" in u.json()["detail"]
        assert SECRET not in json.dumps(main_mod._chat_histories.get(doc_id, []))
        # The redacted PDF itself: text gone.
        d = fitz.open(str(doc_dir / "original.pdf"))
        assert SECRET not in d[0].get_text()
        d.close()
        # History works again for later edits.
        assert (await client.post(f"/api/pdf/{doc_id}/organize/rotate",
                                  json={"pages": [1], "angle": 90})).status_code == 200
        assert (await client.post(f"/api/pdf/{doc_id}/undo")).status_code == 200
        assert _files_containing(doc_dir, SECRET) == []
    finally:
        await client.delete(f"/api/pdf/{doc_id}")


async def test_sanitize_purges_unsanitized_copies(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Visible body text")
    page.insert_text((72, 100), "HIDDENSECRET", render_mode=3)
    doc.set_metadata({"author": "METASECRET"})
    data = doc.tobytes()
    doc.close()
    doc_id = await _upload(client, data)
    doc_dir = main_mod.UPLOAD_DIR / doc_id
    try:
        # Build undo history that still holds the hidden data.
        assert (await client.post(f"/api/pdf/{doc_id}/organize/rotate",
                                  json={"pages": [0], "angle": 90})).status_code == 200
        assert _files_containing(doc_dir, "HIDDENSECRET")
        r = await client.post(f"/api/pdf/{doc_id}/security/sanitize", json={})
        assert r.status_code == 200, r.text
        assert r.json()["history_purged"] is True
        for needle in ("HIDDENSECRET", "METASECRET"):
            assert _files_containing(doc_dir, needle) == [], needle
    finally:
        await client.delete(f"/api/pdf/{doc_id}")


# ─── .env loading ────────────────────────────────────────────────────────────


def test_load_env_files_sets_missing_without_override_and_never_logs_value(tmp_path, monkeypatch, caplog):
    backend_env = tmp_path / "backend.env"
    root_env = tmp_path / "root.env"
    backend_env.write_text("PLATFORM_T_KEY=sk-backend-secret-value\n")
    root_env.write_text("PLATFORM_T_KEY=sk-root-other\nPLATFORM_T_OTHER='quoted val'\nPLATFORM_T_PRESET=fromfile\n")
    monkeypatch.delenv("PLATFORM_T_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_T_OTHER", raising=False)
    monkeypatch.setenv("PLATFORM_T_PRESET", "fromprocess")
    caplog.set_level(logging.DEBUG)
    loaded = advanced_ops.load_env_files([backend_env, root_env, tmp_path / "missing.env"])
    assert loaded == [str(backend_env), str(root_env)]
    assert os.environ["PLATFORM_T_KEY"] == "sk-backend-secret-value"  # first file wins
    assert os.environ["PLATFORM_T_OTHER"] == "quoted val"
    assert os.environ["PLATFORM_T_PRESET"] == "fromprocess"  # override=False
    assert "sk-backend-secret-value" not in caplog.text and "sk-root-other" not in caplog.text


def test_default_env_files_are_backend_then_repo_root():
    assert advanced_ops.DEFAULT_ENV_FILES == (REPO / "backend" / ".env", REPO / ".env")


def _run_child(code: str, env_extra: dict, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "UPLOAD_DIR", "PDF_EDITOR_LOAD_DOTENV")}
    env.update(env_extra)
    out = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout + out.stderr


def test_server_startup_reads_env_file_and_unifies_upload_dir(tmp_path):
    """A real import of backend.main (not under pytest) picks up a .env via
    load_env_files and every module ends up on the same UPLOAD_DIR."""
    up = tmp_path / "custom_uploads"
    env_file = tmp_path / ".env"
    env_file.write_text(f"ANTHROPIC_API_KEY=sk-test-from-dotenv-123\nUPLOAD_DIR={up}\n")
    code = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "import importlib, pathlib\n"
        # Load the temp .env through the startup loader, then re-evaluate
        # advanced_ops (as a fresh process would) before importing main.
        "os.environ['PDF_EDITOR_LOAD_DOTENV'] = '0'\n"
        "from backend import advanced_ops as ao\n"
        "ao.load_env_files([pathlib.Path(%r)])\n"
        "ao = importlib.reload(ao)\n"
        "from backend import main as m\n"
        "from backend.features import forms, objects, sign\n"
        "from backend import document_intelligence as di\n"
        "print('KEYSET', bool(os.environ.get('ANTHROPIC_API_KEY')))\n"
        "print('SAME', m.UPLOAD_DIR == ao.UPLOAD_DIR == di.UPLOAD_DIR == forms.UPLOAD_DIR == objects.UPLOAD_DIR == sign.UPLOAD_DIR)\n"
        "print('DIR', m.UPLOAD_DIR)\n"
    ) % (str(REPO), str(env_file))
    out = _run_child(code, {}, tmp_path)
    assert "KEYSET True" in out
    assert "SAME True" in out, out
    assert f"DIR {up}" in out
    assert "sk-test-from-dotenv-123" not in out


def test_autoload_on_plain_import_reads_repo_env_files(tmp_path):
    """Without pytest in sys.modules, importing advanced_ops loads DEFAULT_ENV_FILES."""
    code = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "from backend import advanced_ops as ao\n"
        "print('AUTOLOAD', ao._should_autoload_env())\n"
        "print('FILES', [str(p) for p in ao.DEFAULT_ENV_FILES if p.is_file()])\n"
        "print('HASKEY', bool(os.environ.get('ANTHROPIC_API_KEY')))\n"
    ) % str(REPO)
    out = _run_child(code, {}, tmp_path)
    assert "AUTOLOAD True" in out
    has_env = (REPO / ".env").is_file() or (REPO / "backend" / ".env").is_file()
    env_has_key = any(
        re.search(r"^\s*ANTHROPIC_API_KEY\s*=\s*\S", p.read_text(), re.M)
        for p in advanced_ops.DEFAULT_ENV_FILES if p.is_file()
    )
    if has_env and env_has_key:
        assert "HASKEY True" in out
    assert "sk-ant" not in out  # never print the key


def test_main_and_advanced_ops_share_upload_dir_object():
    assert main_mod.UPLOAD_DIR is advanced_ops.UPLOAD_DIR
    from backend import document_intelligence as di
    assert di.UPLOAD_DIR is main_mod.UPLOAD_DIR
