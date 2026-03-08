Note: All of the code was written by LLMs: Claude Code and ChatGPT.

# Project Description
mesh-generator is the frontend web application for the mesh planner workflow. It provides the interactive UI for project setup, map operations, optimization controls, and result visualization.

# How to Run It
```bash
npm install
npm run dev
```

The development server runs at http://127.0.0.1:5173 and proxies `/api/v2/*` to `http://127.0.0.1:8000`.

To target another backend:

```bash
BACKEND_URL=http://127.0.0.1:9000 npm run dev
```

To build production assets:

```bash
npm run build
```

# High-Level Implementation Details
The application is built with Vite and modern browser JavaScript modules under `src/`. During development, Vite handles local serving and API proxying; for integrated runtime, the generated `dist/` assets are served by `mesh-backend`.
