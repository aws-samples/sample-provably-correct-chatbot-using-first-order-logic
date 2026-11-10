# Frontend

The React single-page app, built with [Vite](https://vite.dev/) and tested with
[Vitest](https://vitest.dev/). Use npm 11 (`npx -y npm@11 ci`); the lockfile was
written by it.

| Command | What it does |
| --- | --- |
| `npm run dev` | Dev server on http://localhost:5173. It calls the API at `VITE_API_BASE_URL` (default `http://localhost:8000`). |
| `npm test` | Runs the Vitest suite once (jsdom). `npx vitest` watches. |
| `npm run build` | Type-checks, then writes the production bundle to `build/`, with hashed assets under `build/static/`. |
| `npm run preview` | Serves `build/` locally. |

The deployed image builds with `VITE_API_BASE_URL=""`, so the app calls the
API on its own origin. `cdk/docker/server.py` serves `build/` and mounts
`build/static/`, and CloudFront caches `/static/*`, so keep `outDir` and
`assetsDir` in `vite.config.ts` as they are.
