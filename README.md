# Auto-MD

Auto-MD turns codebases and documents into one clean Markdown file or a RAG-ready bundle. It runs as a private local web app: your files are processed on your machine and temporary data is removed when the app exits.

## Run it

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone or download this repository, then run:

```bash
uv run automd.py
```

Auto-MD installs its locked Python environment, starts on `127.0.0.1`, chooses an available port, and opens your browser. Press `Ctrl-C` in the terminal to stop it and clear all job data.

Useful local-development options:

```bash
uv run automd.py --no-browser
uv run automd.py --port 8765
uv run automd.py --log-level info
```

Docker, Node.js, Tesseract, Git, API keys, and cloud services are not required to run the app. Node.js is used only when changing the TypeScript frontend.

## Inputs

- Files and browser-selected folders
- Public GitHub repositories, optionally at a specific ref
- Individual public HTTP(S) web pages
- ZIP and TAR archives, including safely bounded nested archives
- Code, text, Markdown, configuration, JSON/YAML/XML, CSV, and notebooks
- HTML, DOCX, PDF, and common image formats
- Scanned PDFs and images through local English-capable OCR

Smart filtering respects `.gitignore` and `.automdignore` and removes common dependency, cache, build, and generated paths. Advanced settings allow force-includes and additional git-style excludes.

## Outputs

Clean Markdown produces one `.md` file with provenance, stable anchors, a table of contents, and faithfully fenced source content.

RAG bundle produces a ZIP containing:

- the complete Markdown document;
- `chunks.jsonl` with stable IDs, source metadata, heading paths, hashes, and token estimates;
- `manifest.json` with settings, checksums, skips, warnings, and counts.

Chunking follows headings, paragraphs, pages, notebook cells, and code boundaries. Counts are deterministic estimates rather than model-specific token counts.

## Privacy and safety

- The server binds only to loopback and does not support public/LAN hosting.
- Auto-MD makes outbound requests only for URLs you explicitly add.
- Upload names never become server paths without normalization.
- Archive paths, links, entry counts, expanded sizes, PDF pages, image pixels, downloads, and redirects are bounded.
- Remote URLs cannot target loopback, private, or link-local networks.
- Downloads use registered artifact IDs rather than filesystem paths.
- No analytics, CDNs, cookies beyond the local session, or AI APIs are used.

The practical default limits are 512 MiB per job, 100 MiB per file, 20,000 archive entries, 1 GiB expanded data, 500 PDF pages, and 50 megapixels per image.

## Development

Backend:

```bash
uv sync --group dev
uv run ruff check .
uv run mypy
uv run pytest --cov=automd
```

Frontend:

```bash
cd frontend
npm install
npm run check
npm run build
```

## Supported platforms

Auto-MD targets Python 3.11–3.14 on current Windows, macOS, and Linux systems supported by its OCR/PDF wheels. Browser support targets current Chrome, Edge, Firefox, and Safari releases.

## License

[MIT](LICENSE)

