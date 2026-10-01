# AI PDF Editor

A full-featured PDF editor with AI-powered natural language commands. Upload a PDF, edit text, annotate, draw, and chat with your document using Claude AI.

<!-- Screenshot placeholder -->

## Quick Start

### Docker (recommended)

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

docker compose up --build
```

Open [http://localhost:3000](http://localhost:3000).

### Manual Setup

**Backend:**

```bash
cd backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r ../requirements.txt

# Set your API key (optional — chat falls back to regex parsing without it)
export ANTHROPIC_API_KEY=sk-ant-...

uvicorn backend.main:app --reload --port 8000
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Architecture

```
┌────────────────┐     HTTP/REST     ┌────────────────────┐
│   Next.js 16   │ ◄──────────────► │   FastAPI Backend   │
│   React 19     │                   │   PyMuPDF (fitz)    │
│   Zustand      │                   │   Claude AI (opt.)  │
│   Fabric.js    │                   │                     │
│   PDF.js       │                   │   uploads/{uuid}/   │
└────────────────┘                   └────────────────────┘
```

- **Frontend**: Next.js App Router, React 19, Tailwind CSS, Zustand for state, Fabric.js for canvas annotations, PDF.js for rendering
- **Backend**: FastAPI with PyMuPDF for all PDF operations, Claude API for AI chat (optional)

## Features

The editor has an Acrobat-style tool rail on the left. Each mode owns the page while it is active, so only one overlay is live at a time. Single-key shortcuts are shown in brackets, and `?` lists them all.

**AI assistant** (Cmd/Ctrl+/ or the chat button)
- A Claude agent (Sonnet 5.5 by default; Opus 5.5 or Haiku 4.5 from the model menu) that reads the whole document and answers with page citations. Clicking a citation jumps to the page and highlights the quoted passage.
- It edits the PDF for you through the same routes as the tools: rewrite or restyle paragraphs in place, translate the whole document keeping its layout, replace text, fill and create form fields, make a flat form fillable, redact, OCR, compress, rotate, delete, insert or reorder pages, add page numbers, headers, footers, Bates numbers, bookmarks, comments and watermarks, protect and sanitize. Each AI change is one undo step, and the chat shows an Undo button.
- One-click actions: summaries, explain, rewrite, shorten or fix the grammar of a selection, smart redaction with a review list, fill a form from a saved profile or a reference PDF, compare with a reference PDF, export tables to CSV and data to JSON.
- Answers stream as they are written, and Stop cancels a run. Scanned pages are sent to the model as images. Replies are rendered as safe React nodes rather than injected HTML.
- With no API key the chat shows a setup card. A key entered there is kept in server memory, or written to `backend/.env` (mode 600) if you choose to save it. The key is never returned or logged.

**Edit**
- **Edit text** [E]: edit existing text in place by paragraph, line or run. Paragraphs reflow inside their original column. The embedded font is reused (glyphs it lacks fall back to Base-14 one glyph at a time), and size, colour, weight, alignment, letter spacing, word spacing and horizontal scaling are kept. Text at any angle can be edited, moved or deleted. A paragraph that grows pushes the text below it down. When it cannot fit, a dialog offers **Shrink to fit**, **Allow overlap** or **Cancel**.
- **Add text** [T]: click anywhere to add new text, or click an existing text box to rewrite it.
- **Objects** [O]: move, resize, rotate, crop, replace, extract and delete images. Insert images. Draw real vector shapes (rectangle, ellipse, line, arrow). Move or delete vector art. Multi-select with Shift-click, a marquee drag or Cmd/Ctrl+A, then move, nudge (arrows 1pt, Shift 10pt), duplicate (Cmd/Ctrl+D), align, distribute, bring to front, send to back or delete them together as one undo step. Moves are made in place in the content stream, so an object keeps its stacking order under text.
- **Select** [V]: move or resize text blocks.

**Review**
- **Comment** [C]: sticky notes, highlight, underline, strikeout, squiggly, text boxes, callouts, shapes, ink and stamps, saved as real PDF annotations that Acrobat can read. The comments panel has threaded replies (stored as Acrobat-style reply annotations, so no extra icon is drawn on the page), review status and filters.
- Quick **Highlight** [H], **Draw** [D], **Eraser** [Shift+E] and **Ask AI** about a region [S].

**Prepare**
- **Fill & Sign** [G]: a library of drawn, typed or uploaded signatures and initials, plus text, date, check and cross items. You can lock the document after signing.
- **Digital IDs and signatures**: import a CA-issued ID (.p12/.pfx; the key is re-encrypted at rest under your passphrase, which is never stored) or create a self-signed one. PAdES signatures (pyHanko) with an optional RFC 3161 trusted timestamp and an optional LTV mode that embeds revocation data. Verification uses the macOS system roots (certifi on Linux) plus certificates you trust, and can check revocation online. Each signature reports signer, issuer, trust, whether the document changed after signing, timestamp and LTV. Self-signed IDs are honestly reported as untrusted, as Acrobat reports them.
- **Signed-document protection**: an edit to a digitally signed PDF asks first, naming the signer, and offers Continue, Cancel or **Save a copy first**. The check is enforced on the server for every editing route.
- **Forms** [F]: fill every field type, including multi-select list boxes. Create, edit, move and delete fields, including date fields (mm/dd/yyyy). Auto-detect fields on flat forms and on scanned pages (OCR plus line and box detection), with radio groups built from option rows and table header rows left alone. Import and export JSON, FDF or XFDF. Flatten.
- **Redact** [R]: true redaction that removes text, image pixels and vector art. Search by text or regex, with PII presets (SSN, phone, email, card numbers with a Luhn check, dates, money, addresses). Every apply is verified. Applying redactions clears the undo history so no unredacted copy is kept anywhere on the server. The Sanitize tab removes metadata, hidden text, JavaScript, attachments and links.

**Document**
- **Organize** [P]: a thumbnail grid with drag reorder, insert blank pages, insert pages from another file, extract, duplicate, rotate, delete, crop (including auto-crop of white margins), resize (annotations, links and form fields move with the content) and split. Bookmarks with nesting, indent and outdent, and drag-and-drop reordering.
- **Convert**: OCR in any installed Tesseract language, or two at once (for example English plus Spanish). Missing languages can be installed from the panel. OCR can add a searchable text layer or make scanned text **editable** (the original words are erased from the scan and replaced with real text in a matched size and colour). Export to Word, Excel, CSV, Markdown, TXT, PNG, JPG, positioned HTML or reflowable semantic HTML ("Web page"). Create a PDF from images, text, Markdown or DOCX. The DOCX path keeps page size, margins, headers and footers with page numbers, images, tables with borders, shading and merged cells, and lists; on a Mac with Microsoft Word installed it can use Word instead. Compress.
- **Protect**: AES-256 open and permission passwords, as a protected download or as restrictions on the working copy. Remove security.
- **More**: watermark; header and footer with page numbers and Bates numbering (live preview), where every application is a run that can later be edited or removed, including Acrobat-made headers and footers; quick text stamps, bookmarks, flatten, PDF/A-style archival clean-up, compare two documents, AI page actions.

**Everywhere**
- Undo and redo for every change from every tool (Cmd/Ctrl+Z, Cmd/Ctrl+Shift+Z), with an edit-history menu.
- 100% zoom is true physical size. Fit width, Fit page, Actual size and zoom presets are in the toolbar, with Cmd/Ctrl+0, + and -. Pages re-render without a blank flash after an edit.
- Find and replace (Cmd/Ctrl+F), AI chat (Cmd/Ctrl+/), server rendering or PDF.js rendering, dark mode, and a mobile layout.
- Downloads of digitally signed documents are byte-for-byte copies of the stored file, so signatures stay valid.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/pdf/upload` | Upload a PDF file |
| `GET` | `/api/pdf/{id}/info` | Document metadata and page info |
| `GET` | `/api/pdf/{id}/page/{n}` | Render page as PNG |
| `GET` | `/api/pdf/{id}/thumbnail/{n}` | Page thumbnail |
| `GET` | `/api/pdf/{id}/text` | Extract text blocks |
| `POST` | `/api/pdf/{id}/text/edit` | Edit text in bounding box |
| `POST` | `/api/pdf/{id}/text/add` | Add text at coordinates |
| `POST` | `/api/pdf/{id}/text/move` | Move/resize content |
| `POST` | `/api/pdf/{id}/find` | Find text |
| `POST` | `/api/pdf/{id}/replace` | Find and replace |
| `POST` | `/api/pdf/{id}/highlight` | Add highlights |
| `POST` | `/api/pdf/{id}/draw` | Add ink drawings |
| `GET/POST` | `/api/pdf/{id}/annotations/{n}` | Fabric.js annotations |
| `PATCH` | `/api/pdf/{id}/edit` | Rotate or delete page |
| `POST` | `/api/pdf/{id}/reorder` | Reorder pages |
| `POST` | `/api/pdf/{id}/split` | Split into multiple PDFs |
| `POST` | `/api/pdf/{id}/merge` | Merge PDFs |
| `GET` | `/api/pdf/{id}/export` | Download PDF |
| `POST` | `/api/pdf/{id}/ai/assist` | AI text operations |
| `POST` | `/api/pdf/{id}/chat` | Natural language chat (runs the AI agent; regex fallback without a key) |
| `GET` | `/api/ai/status` | AI availability check |
| `GET` | `/api/ai/config` | Key status (never the key), current model and model list |
| `POST/DELETE` | `/api/ai/key` | Set (optionally persist) or forget the API key |
| `POST` | `/api/ai/model` | Switch model |
| `POST` | `/api/ai/chat/stream`, `/api/ai/chat` | AI agent chat (SSE stream or one response) |
| `POST` | `/api/ai/chat/stop` | Stop a running agent turn |
| `POST` | `/api/ai/redactions/apply` | Apply AI-proposed redactions through the real redact route |
| `POST` | `/api/ai/locate` | Find rectangles for a cited quote |
| `DELETE` | `/api/pdf/{id}` | Delete document |

### Editing tools (`backend/advanced_ops.py`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/pdf/{id}/undo`, `/redo` | Step through snapshot history |
| `GET` | `/api/pdf/{id}/history` | List undo/redo states |
| `POST` | `/api/pdf/{id}/watermark`, `/stamp` | Text watermark / positioned text stamp |
| `POST` | `/api/pdf/{id}/flatten` | Bake annotations and form fields into the page (pending redaction marks are kept) |
| `POST` | `/api/pdf/{id}/convert-pdfa` | Archival clean-up (fonts, scripts, form values baked) |
| `POST` | `/api/pdf/compare` | Text diff of two uploaded documents |
| `GET/POST/DELETE` | `/api/pdf/{id}/images`, `/add-image`, `/image/{page}/{i}` | Legacy image operations |

### Feature modules (`backend/features/`)

Every rect and point is in PDF points with a top-left origin, in the page as displayed (rotation applied). That is the same space as `/info` page width and height. Every change takes an undo snapshot first.

| Module | Endpoints (prefix `/api/pdf`) |
|--------|-------------------------------|
| `text_edit.py` | `GET /{id}/text-edit/page/{n}`; `POST /{id}/text-edit/edit`, `/move`, `/delete` |
| `objects.py` | `GET /{id}/objects/{n}`; `POST /{id}/objects/batch`, `/arrange`; `POST /{id}/objects/image/move`, `/rotate`, `/delete`, `/crop`, `/replace`, `/insert`; `GET /{id}/objects/image/{xref}/extract`; `POST /{id}/objects/drawing/move`, `/delete`; `POST /{id}/objects/shape` |
| `forms.py` | `GET/POST /{id}/form-fields`; `PATCH/DELETE /{id}/form-fields/{fid}`; `POST /{id}/form-fields/fill`, `/flatten`, `/detect`, `/import`; `GET /{id}/form-fields/export` |
| `sign.py` | `POST /{id}/sign/apply`, `/stamp`, `/lock`, `/digital`; `GET /{id}/sign/validate`; `POST/GET/DELETE /signing/certificates[/{cert}]`; `POST /signing/certificates/import`; `GET/POST /signing/trusted`, `DELETE /signing/trusted/{sha256}`; `GET /signing/settings`; `POST /signing/validate` (`?fetch_revocation=true` on both validate routes) |
| `redact.py` | `GET /{id}/redact/words/{n}`; `POST /{id}/redact/mark`, `/apply`, `/search`; `GET/DELETE /{id}/redact/marks`; `GET /redact/presets`; `GET /{id}/security/audit`; `POST /{id}/security/sanitize`, `/protect`, `/unlock` |
| `convert.py` | `GET /ocr/languages`; `POST /ocr/languages/install`; `GET /convert/capabilities`; `GET /{id}/ocr/detect`; `POST /{id}/ocr` and `GET /ocr/jobs/{job}`; `GET /{id}/export/{docx,txt,md,html,png,jpg,xlsx,csv}` (`html?layout=reflow` for semantic HTML); `POST /create`; `POST /{id}/compress` |
| `organize.py` | `POST /{id}/organize/insert-blank`, `insert-file`, `extract`, `duplicate`, `rotate`, `delete`, `crop`, `resize`, `split`, `page-numbers`, `bates`, `header-footer[/preview]`; header/footer runs (`GET/DELETE /{id}/organize/header-footer/runs`, `PUT/DELETE .../runs/{run_id}`); bookmarks (`GET/PUT/POST /{id}/organize/bookmarks`, `PATCH/DELETE .../{i}`, `POST .../{i}/move`, `/indent`, `/outdent`); comments (`GET/POST /{id}/organize/comments`, `.../{xref}/reply`, `.../{xref}/status`, `PATCH/DELETE .../{xref}`); `GET /{id}/organize/stamps` |

Editing routes under `/api/pdf/{id}/` answer `409 {code: "signed_document"}` on a digitally signed PDF unless the request sends `X-Allow-Break-Signature: 1`.

Interactive docs for every route are at `http://localhost:8000/docs`.

### System requirements

OCR needs the Tesseract binary and its language data. On macOS run `brew install tesseract` (add `tesseract-lang` for more languages); the Docker image installs `tesseract-ocr` with English and 13 common language packs. Languages installed from the UI go to `backend/tessdata` (or `PDF_EDITOR_TESSDATA_DIR`). Without Tesseract, `/ocr` returns a clear 400 or 503 error and everything else keeps working.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key for AI features. `backend/.env`, then the repo `.env`, are loaded at startup; real environment variables win |
| `AI_MODEL` | `claude-sonnet-5-5` | Default model (`claude-opus-5-5`, `claude-haiku-4-5`) |
| `AI_EFFORT` | per model | Effort override (`low`, `medium`, `high`, `xhigh`, `max`; ignored on Haiku) |
| `AI_FALLBACKS` | `1` | Server-side refusal fallbacks on Sonnet/Opus (`0` to turn off) |
| `AI_MAX_ITERATIONS` | `25` | Maximum agent tool rounds per message |
| `AI_MAX_TOKENS` | `32000` | Maximum output tokens per model call |
| `AI_FULL_TEXT_CHARS` | `400000` | Documents longer than this get an outline plus retrieval instead of the full text |
| `AI_OCR_TIMEOUT` | `240` | Seconds the agent's OCR tool may run |
| `PDF_EDITOR_TESSDATA_DIR` | `backend/tessdata` | Where OCR languages installed from the UI are stored |
| `PDF_EDITOR_LOAD_DOTENV` | `1` (`0` under pytest) | Force `.env` loading on or off |
| `UPLOAD_DIR` | `uploads` | Directory for uploaded PDFs |
| `MAX_FILE_SIZE_MB` | `50` | Maximum upload file size |
| `FILE_TTL_HOURS` | `24` | Auto-delete files after this many hours |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |
| `AI_RATE_LIMIT_RPM` | `30` | AI endpoint rate limit (requests/minute) |
| `MAX_TEXT_INPUT_LENGTH` | `10000` | Max characters for text inputs |

## Development

### Run tests

```bash
# Backend
pip install -r requirements.txt
pytest backend/tests/ -v

# Frontend
cd frontend
npm install
npm test
```

### Project structure

```
├── backend/
│   ├── main.py              # FastAPI server
│   ├── ai_engine.py          # Claude AI integration
│   ├── advanced_ops.py        # Advanced PDF operations
│   ├── document_intelligence.py # Document analysis
│   ├── Dockerfile
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── app/               # Next.js App Router
│   │   ├── components/        # React components
│   │   └── lib/               # API client + Zustand store
│   ├── Dockerfile
│   └── package.json
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request
