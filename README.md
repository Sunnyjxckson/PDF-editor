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

- Upload and view PDFs with page thumbnails
- Edit, add, and move text
- Find and replace across all pages
- Highlight and freehand drawing annotations
- Rotate, delete, reorder, and split pages
- AI chat: natural language commands ("replace X with Y", "delete page 3", "extract all emails")
- Export with flattened annotations
- Dark mode, keyboard shortcuts, mobile-responsive

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
