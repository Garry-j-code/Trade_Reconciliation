# Trade Reconciliation dashboard

Local React (Vite + TSX + Recharts) ops console. Talks to `uv run serve-api`.

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The Vite proxy forwards `/api` and `/health` to `http://127.0.0.1:8000`. To call the API directly, copy `.env.example` to `.env` and set `VITE_API_BASE=http://127.0.0.1:8000` (API CORS already allows this origin).

Bedrock is optional. The agent panel renders an empty placeholder until a suggestion exists; Investigate shows a clear error if Bedrock is unavailable.
