"""sign2: CA-issued digital IDs (.p12 import), RFC 3161 timestamps, LTV/revocation
and the validation trust store.

No test touches the internet:
  * the TSA is pyHanko's DummyTimeStamper (or a closed localhost port for the
    failure path);
  * CRLs are served by a throw-away HTTP server on 127.0.0.1, so the REAL
    aiohttp fetching code path runs end to end.
Every assertion that matters re-opens the resulting PDF from disk.
"""

import io
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops
from backend.features import sign as sign_mod

PASS = "s3cret-Passphrase!"


# ─── PKI helpers ─────────────────────────────────────────────────────────────


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str, org: str = "Test PKI") -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])


class PKI:
    """Root CA -> leaf signer (+ optional TSA), with a CRL served over localhost HTTP."""

    def __init__(self, crl_url: str | None = None, expired: bool = False):
        now = datetime.now(timezone.utc)
        self.ca_key = _key()
        self.ca_name = _name("Test Root CA")
        self.ca = (
            x509.CertificateBuilder().subject_name(self.ca_name).issuer_name(self.ca_name)
            .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()), critical=False)
            .sign(self.ca_key, hashes.SHA256())
        )
        self.leaf_key = _key()
        nb, na = (now - timedelta(days=30), now - timedelta(days=1)) if expired else (now - timedelta(hours=1), now + timedelta(days=365))
        b = (
            x509.CertificateBuilder().subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "Casey Trusted"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Acme Corp"),
                x509.NameAttribute(NameOID.EMAIL_ADDRESS, "casey@acme.test"),
            ])).issuer_name(self.ca_name)
            .public_key(self.leaf_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(nb).not_valid_after(na)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False)
        )
        if crl_url:
            b = b.add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier(crl_url)], relative_name=None, reasons=None,
                crl_issuer=None)]), critical=False)
        self.leaf = b.sign(self.ca_key, hashes.SHA256())

        # TSA cert (for DummyTimeStamper), issued by the same CA
        self.tsa_key = _key()
        self.tsa = (
            x509.CertificateBuilder().subject_name(_name("Test TSA")).issuer_name(self.ca_name)
            .public_key(self.tsa_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(self.ca_key, hashes.SHA256())
        )

    def crl(self, revoked: bool = False) -> bytes:
        now = datetime.now(timezone.utc)
        b = (x509.CertificateRevocationListBuilder().issuer_name(self.ca_name)
             .last_update(now - timedelta(minutes=5)).next_update(now + timedelta(days=7))
             .add_extension(x509.CRLNumber(1), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),
                            critical=False))
        if revoked:
            b = b.add_revoked_certificate(
                x509.RevokedCertificateBuilder().serial_number(self.leaf.serial_number)
                .revocation_date(now - timedelta(minutes=1)).build())
        return b.sign(self.ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)

    def p12(self, password: str | None = PASS, include_ca: bool = True) -> bytes:
        enc = (serialization.BestAvailableEncryption(password.encode()) if password
               else serialization.NoEncryption())
        return pkcs12.serialize_key_and_certificates(b"casey", self.leaf_key, self.leaf,
                                                     [self.ca] if include_ca else None, enc)

    def ca_der(self) -> bytes:
        return self.ca.public_bytes(serialization.Encoding.DER)

    def dummy_tsa(self):
        from asn1crypto import keys as asn1_keys
        from asn1crypto import x509 as asn1_x509
        from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper
        from pyhanko_certvalidator.registry import SimpleCertificateStore

        tsa_cert = asn1_x509.Certificate.load(self.tsa.public_bytes(serialization.Encoding.DER))
        tsa_key = asn1_keys.PrivateKeyInfo.load(self.tsa_key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        ca = asn1_x509.Certificate.load(self.ca_der())
        return DummyTimeStamper(tsa_cert=tsa_cert, tsa_key=tsa_key,
                                certs_to_embed=SimpleCertificateStore.from_certs([ca]))


class CRLServer:
    """Serves /ca.crl from 127.0.0.1 (the real aiohttp fetcher hits it)."""

    def __init__(self):
        self.body = b""
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/pkix-crl")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/ca.crl"
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sign_mod, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def sclient(upload_dir):
    app = FastAPI()
    app.include_router(sign_mod.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


@pytest.fixture
def crl_server():
    s = CRLServer()
    yield s
    s.close()


def _make_doc(upload_dir: Path) -> str:
    doc_id = str(uuid.uuid4())
    (upload_dir / doc_id).mkdir()
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 72), "Purchase agreement", fontsize=12)
    doc.save(str(upload_dir / doc_id / "original.pdf"))
    doc.close()
    return doc_id


def _pdf(upload_dir, doc_id) -> bytes:
    return (upload_dir / doc_id / "original.pdf").read_bytes()


def _history_len(upload_dir: Path, doc_id: str) -> int:
    hf = upload_dir / doc_id / "history.json"
    return len(json.loads(hf.read_text())["versions"]) if hf.exists() else 0


async def _import(sclient, pki: PKI, password=PASS, **kw) -> dict:
    r = await sclient.post("/api/pdf/signing/certificates/import",
                           files={"file": ("id.p12", pki.p12(password, **kw), "application/x-pkcs12")},
                           data={"passphrase": password or "new-local-pass"})
    assert r.status_code == 200, r.text
    return r.json()


async def _trust(sclient, der: bytes):
    r = await sclient.post("/api/pdf/signing/trusted", files={"file": ("ca.cer", der, "application/pkix-cert")})
    assert r.status_code == 200, r.text
    return r.json()


async def _sign(sclient, doc_id, cert_id, **kw):
    body = {"cert_id": cert_id, "passphrase": PASS, "page": 0, "rect": [72, 600, 272, 680], **kw}
    return await sclient.post(f"/api/pdf/{doc_id}/sign/digital", json=body)


def _embedded_sig(data: bytes):
    from pyhanko.pdf_utils.reader import PdfFileReader

    return PdfFileReader(io.BytesIO(data), strict=False).embedded_signatures[0]


# ─── 1. Import a real (CA-issued) digital ID ─────────────────────────────────


async def test_import_p12_stores_key_encrypted_and_never_the_passphrase(sclient, upload_dir):
    pki = PKI()
    info = await _import(sclient, pki)
    assert info["source"] == "imported" and info["self_signed"] is False
    assert info["name"] == "Casey Trusted" and info["organization"] == "Acme Corp"
    assert info["email"] == "casey@acme.test" and info["issuer"] == "Test Root CA"
    assert info["chain_length"] == 1 and info["passphrase_protected"] is True

    d = upload_dir / "_certs" / info["cert_id"]
    key_pem = (d / "key.pem").read_bytes()
    assert key_pem.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    assert oct((d / "key.pem").stat().st_mode & 0o777) == "0o600"
    # The passphrase is not written anywhere under _certs
    for f in (upload_dir / "_certs").rglob("*"):
        if f.is_file():
            assert PASS.encode() not in f.read_bytes(), f
    # The raw key material is not on disk in the clear, and it does not open without the passphrase
    raw_der = pki.leaf_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
    assert raw_der not in key_pem
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(key_pem, password=None)
    k = serialization.load_pem_private_key(key_pem, password=PASS.encode())
    assert k.private_numbers() == pki.leaf_key.private_numbers()
    # Chain (the CA) stored for embedding in signatures
    assert b"BEGIN CERTIFICATE" in (d / "chain.pem").read_bytes()
    r = await sclient.get(f"/api/pdf/signing/certificates/{info['cert_id']}")
    assert r.json()["source"] == "imported"


async def test_import_rejects_wrong_passphrase_non_p12_and_expired(sclient, upload_dir):
    pki = PKI()
    r = await sclient.post("/api/pdf/signing/certificates/import",
                           files={"file": ("id.p12", pki.p12(), "application/x-pkcs12")}, data={"passphrase": "nope"})
    assert r.status_code == 403
    r = await sclient.post("/api/pdf/signing/certificates/import",
                           files={"file": ("ca.cer", pki.ca_der(), "application/pkix-cert")}, data={"passphrase": PASS})
    assert r.status_code == 403
    r = await sclient.post("/api/pdf/signing/certificates/import",
                           files={"file": ("id.p12", PKI(expired=True).p12(), "application/x-pkcs12")},
                           data={"passphrase": PASS})
    assert r.status_code == 400 and "expired" in r.json()["detail"]
    certs = upload_dir / "_certs"
    assert not certs.exists() or not any(p.name != "_trusted" for p in certs.iterdir())  # nothing half-stored


async def test_import_unprotected_p12_is_protected_at_rest(sclient, upload_dir):
    pki = PKI()
    info = await _import(sclient, pki, password=None)
    key_pem = (upload_dir / "_certs" / info["cert_id"] / "key.pem").read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in key_pem
    serialization.load_pem_private_key(key_pem, password=b"new-local-pass")


async def test_sign_with_imported_id_requires_passphrase_and_embeds_chain(sclient, upload_dir):
    pki = PKI()
    info = await _import(sclient, pki)
    doc_id = _make_doc(upload_dir)
    before = _pdf(upload_dir, doc_id)
    r = await _sign(sclient, doc_id, info["cert_id"], passphrase=None)
    assert r.status_code == 400
    r = await _sign(sclient, doc_id, info["cert_id"], passphrase="wrong")
    assert r.status_code == 403
    assert _pdf(upload_dir, doc_id) == before and _history_len(upload_dir, doc_id) == 0
    r = await _sign(sclient, doc_id, info["cert_id"])
    assert r.status_code == 200, r.text
    assert r.json()["self_signed"] is False and r.json()["timestamped"] is False

    data = _pdf(upload_dir, doc_id)
    doc = fitz.open(stream=data, filetype="pdf")
    w = list(doc[0].widgets())
    assert len(w) == 1 and w[0].is_signed
    doc.close()
    sig = _embedded_sig(data)
    assert sig.signer_cert.subject.native["common_name"] == "Casey Trusted"
    embedded = {c.chosen.subject.native.get("common_name") for c in sig.signed_data["certificates"]}
    assert "Test Root CA" in embedded  # chain shipped in the CMS, like Acrobat does


# ─── 4. Trust store ──────────────────────────────────────────────────────────


async def test_ca_issued_signature_trusted_only_after_user_trusts_root(sclient, upload_dir):
    """KEY TEST (mutation-proved): trust comes from the user-added CA."""
    pki = PKI()
    info = await _import(sclient, pki)
    doc_id = _make_doc(upload_dir)
    assert (await _sign(sclient, doc_id, info["cert_id"])).status_code == 200

    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["intact"] and s["valid"] and s["modified_after_signing"] is False
    assert s["trusted"] is False and s["trust_problem"] == "unknown_issuer"
    assert s["issuer"].endswith("Test Root CA") and s["self_signed"] is False
    assert "issuer is not in the trust store" in s["summary"]

    added = await _trust(sclient, pki.ca_der())
    fp = added["added"][0]["fingerprint_sha256"]
    lst = (await sclient.get("/api/pdf/signing/trusted")).json()
    assert [u["fingerprint_sha256"] for u in lst["user"]] == [fp] and lst["user"][0]["is_ca"] is True

    v = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()
    s = v["signatures"][0]
    assert s["trusted"] is True and s["trust_source"] == "user" and s["trust_problem"] is None
    assert "Test Root CA" in s["trust_anchor"]
    assert s["summary"] == "Valid: document unchanged since signing"
    assert v["trust_store"]["user_certificates"] == 1

    # Untrust again -> back to untrusted
    assert (await sclient.delete(f"/api/pdf/signing/trusted/{fp}")).status_code == 200
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["trusted"] is False


async def test_self_signed_app_id_is_reported_untrusted(sclient, upload_dir):
    """The old code silently trusted every app-made ID; Acrobat does not, so neither do we."""
    r = await sclient.post("/api/pdf/signing/certificates", json={"name": "Self Signer"})
    cert = r.json()
    assert cert["source"] == "self_signed" and cert["self_signed"] is True
    doc_id = _make_doc(upload_dir)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                           json={"cert_id": cert["cert_id"], "page": 0, "rect": [72, 600, 272, 680]})
    assert r.status_code == 200 and r.json()["self_signed"] is True
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["intact"] and s["valid"]
    assert s["trusted"] is False and s["trust_problem"] == "self_signed" and s["issued_by_this_app"] is True
    assert "self-signed" in s["summary"]


async def test_trusted_upload_rejects_garbage_and_accepts_pem(sclient, upload_dir):
    r = await sclient.post("/api/pdf/signing/trusted", files={"file": ("x.cer", b"not a cert", "x")})
    assert r.status_code == 400
    pem = PKI().ca.public_bytes(serialization.Encoding.PEM)
    r = await sclient.post("/api/pdf/signing/trusted", files={"file": ("x.pem", pem, "x")})
    assert r.status_code == 200 and len(r.json()["added"]) == 1
    assert (await sclient.delete("/api/pdf/signing/trusted/zz")).status_code == 400
    assert (await sclient.delete("/api/pdf/signing/trusted/" + "0" * 64)).status_code == 404


def test_system_roots_loaded_with_certifi_fallback(monkeypatch):
    monkeypatch.setattr(sign_mod, "_SYSTEM_ROOTS", None)
    roots, source = sign_mod._system_roots()
    assert len(roots) > 50 and source in ("macos-system-roots", "certifi")
    monkeypatch.setattr(sign_mod, "_SYSTEM_ROOTS", None)
    monkeypatch.setattr(sign_mod, "_load_macos_roots", lambda: [])
    roots, source = sign_mod._system_roots()
    assert source == "certifi" and len(roots) > 50
    monkeypatch.setattr(sign_mod, "_SYSTEM_ROOTS", None)


# ─── 2. Timestamps ───────────────────────────────────────────────────────────


async def test_timestamp_off_by_default_and_on_with_custom_tsa(sclient, upload_dir, monkeypatch):
    pki = PKI()
    calls = []

    def fake_ts(url):
        calls.append(url)
        return pki.dummy_tsa()

    monkeypatch.setattr(sign_mod, "_make_timestamper", fake_ts)
    info = await _import(sclient, pki)

    plain = _make_doc(upload_dir)
    assert (await _sign(sclient, plain, info["cert_id"])).status_code == 200
    assert calls == []  # OFF by default: no TSA contacted
    s = (await sclient.get(f"/api/pdf/{plain}/sign/validate")).json()["signatures"][0]
    assert s["timestamped"] is False and s["timestamp_time"] is None

    doc_id = _make_doc(upload_dir)
    r = await _sign(sclient, doc_id, info["cert_id"], timestamp=True, tsa_url="https://tsa.example.test/rfc3161")
    assert r.status_code == 200, r.text
    assert calls == ["https://tsa.example.test/rfc3161"] and r.json()["timestamped"] is True

    # Real PDF: the CMS carries an RFC 3161 signature-time-stamp-token unsigned attribute
    sig = _embedded_sig(_pdf(upload_dir, doc_id))
    unsigned = sig.signer_info["unsigned_attrs"]
    assert any(a["type"].dotted == "1.2.840.113549.1.9.16.2.14" for a in unsigned)

    await _trust(sclient, pki.ca_der())
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["timestamped"] is True and s["timestamp_time"]
    assert s["timestamp_valid"] is True and s["timestamp_trusted"] is True


async def test_timestamp_default_url_and_bad_url(sclient, upload_dir, monkeypatch):
    pki = PKI()
    calls = []
    monkeypatch.setattr(sign_mod, "_make_timestamper", lambda u: calls.append(u) or pki.dummy_tsa())
    info = await _import(sclient, pki)
    doc_id = _make_doc(upload_dir)
    assert (await _sign(sclient, doc_id, info["cert_id"], timestamp=True, tsa_url="ftp://x")).status_code == 400
    assert (await _sign(sclient, doc_id, info["cert_id"], timestamp=True)).status_code == 200
    assert calls == ["http://timestamp.digicert.com"]


async def test_unreachable_tsa_fails_cleanly_without_touching_document(sclient, upload_dir, monkeypatch):
    import socket

    from pyhanko.sign.timestamps import HTTPTimeStamper

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    monkeypatch.setattr(sign_mod, "_make_timestamper", lambda u: HTTPTimeStamper(f"http://127.0.0.1:{port}/", timeout=2))
    info = await _import(sclient, PKI())
    doc_id = _make_doc(upload_dir)
    before = _pdf(upload_dir, doc_id)
    r = await _sign(sclient, doc_id, info["cert_id"], timestamp=True)
    assert r.status_code == 502 and "Timestamp server" in r.json()["detail"]
    assert _pdf(upload_dir, doc_id) == before
    assert _history_len(upload_dir, doc_id) == 0  # no stray undo step


# ─── 3. LTV + revocation ─────────────────────────────────────────────────────


async def test_ltv_embeds_revocation_info_in_dss(sclient, upload_dir, crl_server):
    pki = PKI(crl_url=crl_server.url)
    crl_server.body = pki.crl()
    info = await _import(sclient, pki)
    await _trust(sclient, pki.ca_der())
    doc_id = _make_doc(upload_dir)
    r = await _sign(sclient, doc_id, info["cert_id"], ltv=True)
    assert r.status_code == 200, r.text
    assert crl_server.hits >= 1

    data = _pdf(upload_dir, doc_id)
    doc = fitz.open(stream=data, filetype="pdf")
    cat = doc.pdf_catalog()
    kind, dss_ref = doc.xref_get_key(cat, "DSS")
    assert kind == "xref", (kind, dss_ref)
    dss_xref = int(dss_ref.split()[0])
    assert doc.xref_get_key(dss_xref, "CRLs")[0] == "array"
    doc.close()
    assert b"/ETSI.CAdES.detached" in data

    crl_server.hits = 0
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["ltv"] is True and s["trusted"] is True and s["intact"] and s["valid"]
    assert crl_server.hits == 0  # validation offline by default


async def test_ltv_refused_when_chain_not_trusted_or_self_signed(sclient, upload_dir, crl_server):
    pki = PKI(crl_url=crl_server.url)
    crl_server.body = pki.crl()
    info = await _import(sclient, pki)  # CA NOT trusted
    doc_id = _make_doc(upload_dir)
    before = _pdf(upload_dir, doc_id)
    r = await _sign(sclient, doc_id, info["cert_id"], ltv=True)
    assert r.status_code == 422 and "LTV" in r.json()["detail"]
    selfid = (await sclient.post("/api/pdf/signing/certificates", json={"name": "Me"})).json()
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                           json={"cert_id": selfid["cert_id"], "page": 0, "ltv": True})
    assert r.status_code == 422
    assert _pdf(upload_dir, doc_id) == before and _history_len(upload_dir, doc_id) == 0


async def test_ltv_refused_when_no_revocation_source(sclient, upload_dir):
    pki = PKI(crl_url=None)  # cert publishes no CRL/OCSP
    info = await _import(sclient, pki)
    await _trust(sclient, pki.ca_der())
    doc_id = _make_doc(upload_dir)
    r = await _sign(sclient, doc_id, info["cert_id"], ltv=True)
    assert r.status_code == 422


async def test_revocation_checked_only_when_requested(sclient, upload_dir, crl_server):
    pki = PKI(crl_url=crl_server.url)
    crl_server.body = pki.crl(revoked=False)
    info = await _import(sclient, pki)
    await _trust(sclient, pki.ca_der())
    doc_id = _make_doc(upload_dir)
    assert (await _sign(sclient, doc_id, info["cert_id"])).status_code == 200
    # The CA now revokes the signer
    crl_server.body = pki.crl(revoked=True)

    v = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()
    s = v["signatures"][0]
    assert v["revocation_checked"] is False and crl_server.hits == 0
    assert s["revoked"] is False and s["trusted"] is True  # offline: not checked

    v = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate", params={"fetch_revocation": "true"})).json()
    s = v["signatures"][0]
    assert v["revocation_checked"] is True and crl_server.hits >= 1
    assert s["revoked"] is True and s["trusted"] is False and s["trust_problem"] == "revoked"
    assert s["summary"].startswith("INVALID") and "revoked" in s["summary"]

    # Uploaded-file validation honours the same toggle
    up = await sclient.post("/api/pdf/signing/validate", params={"fetch_revocation": "true"},
                            files={"file": ("s.pdf", _pdf(upload_dir, doc_id), "application/pdf")})
    assert up.json()["signatures"][0]["revoked"] is True


async def test_settings_endpoint(sclient, upload_dir):
    s = (await sclient.get("/api/pdf/signing/settings")).json()
    assert s["default_tsa_url"] == "http://timestamp.digicert.com"
    assert s["system_roots"]["count"] > 50
