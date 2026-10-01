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

**Edit**
- **Edit text** [E]: edit existing text in place by paragraph, line or run. Paragraphs reflow inside their original column. The embedded font is reused, and size, colour, weight and alignment are kept. You can also move or delete text and change its font, size, colour, bold or italic.
- **Add text** [T]: click anywhere to add new text, or click an existing text box to rewrite it.
- **Objects** [O]: move, resize, rotate, crop, replace, extract and delete images. Insert images. Draw real vector shapes (rectangle, ellipse, line, arrow). Move or delete vector art.
- **Select** [V]: move or resize text blocks.

**Review**
- **Comment** [C]: sticky notes, highlight, underline, strikeout, squiggly, text boxes, callouts, shapes, ink and stamps, saved as real PDF annotations that Acrobat can read. The comments panel has threaded replies, review status and filters.
- Quick **Highlight** [H], **Draw** [D], **Eraser** [Shift+E] and **Ask AI** about a region [S].

**Prepare**
- **Fill & Sign** [G]: a library of drawn, typed or uploaded signatures and initials, plus text, date, check and cross items. You can lock the document after signing. Self-signed digital IDs and PAdES digital signatures (pyHanko), with signature verification for this or any uploaded PDF.
- **Forms** [F]: fill every field type. Create, edit, move and delete fields. Auto-detect fields on flat forms. Import and export JSON, FDF or XFDF. Flatten.
- **Redact** [R]: true redaction that removes text, image pixels and vector art. Search by text or regex, with PII presets (SSN, phone, email, card numbers with a Luhn check, dates, money, addresses). Every apply is verified. The Sanitize tab removes metadata, hidden text, JavaScript, attachments and links.

**Document**
- **Organize** [P]: a thumbnail grid with drag reorder, insert blank pages, insert pages from another file, extract, duplicate, rotate, delete, crop (including auto-crop of white margins), resize and split. Also bookmarks.
- **Convert**: OCR that adds a searchable or editable text layer (Tesseract), plus export to Word, Excel, CSV, Markdown, HTML, TXT, PNG or JPG. Create a PDF from images, text, Markdown or DOCX. Compress.
- **Protect**: AES-256 open and permission passwords, as a protected download or as restrictions on the working copy. Remove security.
- **More**: watermark, header and footer with page numbers and Bates numbering (live preview), quick text stamps, bookmarks, flatten, PDF/A-style archival clean-up, compare two documents, AI page actions.

**Everywhere**
- Undo and redo for every change from every tool (Cmd/Ctrl+Z, Cmd/Ctrl+Shift+Z), with an edit-history menu.
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
| `POST` | `/api/pdf/{id}/chat` | Natural language chat |
| `GET` | `/api/ai/status` | AI availability check |
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
| `objects.py` | `GET /{id}/objects/{n}`; `POST /{id}/objects/image/move`, `/rotate`, `/delete`, `/crop`, `/replace`, `/insert`; `GET /{id}/objects/image/{xref}/extract`; `POST /{id}/objects/drawing/move`, `/delete`; `POST /{id}/objects/shape` |
| `forms.py` | `GET/POST /{id}/form-fields`; `PATCH/DELETE /{id}/form-fields/{fid}`; `POST /{id}/form-fields/fill`, `/flatten`, `/detect`, `/import`; `GET /{id}/form-fields/export` |
| `sign.py` | `POST /{id}/sign/apply`, `/stamp`, `/lock`, `/digital`; `GET /{id}/sign/validate`; `POST/GET/DELETE /signing/certificates[/{cert}]`; `POST /signing/validate` |
| `redact.py` | `GET /{id}/redact/words/{n}`; `POST /{id}/redact/mark`, `/apply`, `/search`; `GET/DELETE /{id}/redact/marks`; `GET /redact/presets`; `GET /{id}/security/audit`; `POST /{id}/security/sanitize`, `/protect`, `/unlock` |
| `convert.py` | `GET /ocr/languages`; `GET /{id}/ocr/detect`; `POST /{id}/ocr` and `GET /ocr/jobs/{job}`; `GET /{id}/export/{docx,txt,md,html,png,jpg,xlsx,csv}`; `POST /create`; `POST /{id}/compress` |
| `organize.py` | `POST /{id}/organize/insert-blank`, `insert-file`, `extract`, `duplicate`, `rotate`, `delete`, `crop`, `resize`, `split`, `page-numbers`, `bates`, `header-footer[/preview]`; bookmarks (`GET/PUT/POST /{id}/organize/bookmarks`, `PATCH/DELETE .../{i}`); comments (`GET/POST /{id}/organize/comments`, `.../{xref}/reply`, `.../{xref}/status`, `PATCH/DELETE .../{xref}`); `GET /{id}/organize/stamps` |

Interactive docs for every route are at `http://localhost:8000/docs`.

### System requirements

OCR needs the Tesseract binary and its language data. On macOS run `brew install tesseract`; the Docker image installs `tesseract-ocr`. Without Tesseract, `/ocr` returns a clear 400 or 503 error and everything else keeps working.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key for AI features |
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
