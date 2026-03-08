# mesh-generator (Frontend)

`mesh-generator` is now the frontend-only web UI for the planner workflow.

## Requirements

- Node.js 20+
- `mesh-backend` running with `/api/v2` endpoints

## Development

```bash
npm install
npm run dev
```

The Vite dev server listens on `http://127.0.0.1:5173` and proxies `/api/v2/*` to
`http://127.0.0.1:8000` by default.

To target another backend URL:

```bash
BACKEND_URL=http://127.0.0.1:9000 npm run dev
```

## Production Build

```bash
npm run build
```

Build output is generated in `dist/`.

## Production-like Run With mesh-backend

1. Build the frontend in this repo:
   ```bash
   npm run build
   ```
2. Run `mesh-backend` with `FRONTEND_DIST_DIR` pointing at this build output:
   ```bash
   FRONTEND_DIST_DIR=/Users/timur/Documents/src/LoraMeshPlanner/mesh-generator/dist uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

Then open `http://127.0.0.1:8000`.
