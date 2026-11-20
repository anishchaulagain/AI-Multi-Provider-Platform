# Frontend

Next.js 16 (App Router) web app for the platform, built with React 19, TypeScript, Tailwind 4 and shadcn/ui. It talks to the Platform API at `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`).

## Pages

| Route | Description |
|---|---|
| `/login` | Sign in (JWT); the seeded user is `admin@example.com` / `admin12345` |
| `/app` | Overview: account and LLM usage for the last 24 hours |
| `/app/chat` | Conversations: streaming answers with clickable `[n]` citations, an optional document filter, history token savings, and creating a capsule |
| `/app/documents` | Upload, live ingestion status over SSE, chunk preview, delete. The uploaded file isn't kept after processing; to redo a document, upload it again |
| `/app/playground` | Call any gateway alias directly |
| `/app/api-keys` | Create and revoke API keys |
| `/app/capsules/[token]` | Read-only shared context capsule, with a button to continue it as a new conversation |

All API calls go through [`lib/api.ts`](lib/api.ts); the session is held by [`lib/auth.tsx`](lib/auth.tsx).

## Run

```bash
cp .env.example .env.local   # or `make env` from the repo root
npm ci
npm run dev                  # http://localhost:3000
```

The backend must be running (`make up && make migrate && make seed`, then `make dev` from the repo root starts everything).

```bash
npm run lint
npm run build
```

> This Next.js version has breaking changes from older releases. Check `node_modules/next/dist/docs/` before changing routing, data fetching or config (see [AGENTS.md](AGENTS.md)).
