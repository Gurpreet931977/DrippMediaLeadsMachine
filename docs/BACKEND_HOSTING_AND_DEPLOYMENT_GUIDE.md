# Backend Hosting and Deployment Guide
**Dripp Media International Leads Machine — Phase 12.2**

## 1. System Architecture Overview

The system architecture cleanly decouples presentation and persistent orchestration:

```
┌──────────────────────────────────────────────┐
│        Vercel Static Edge Frontend           │
│  https://dripp-media-leads-machine.vercel.app │
│  - Pure Static SPA (HTML5 / CSS / Vanilla JS)│
│  - No backend credentials / No Serverless fn  │
└──────────────────────┬───────────────────────┘
                       │ HTTPS (CORS + Bearer Session)
                       ▼
┌──────────────────────────────────────────────┐
│        Hosted FastAPI Python Backend         │
│  e.g. https://api.drippmedia.com or Render   │
│  - FastAPI / Uvicorn (Python 3.11+)          │
│  - In-Memory Operator Session Manager (2h)   │
│  - Persistent Disk (/app/data for run logs)  │
│  - gspread Service Account CRM Driver        │
│  - Safety Locks (Travel Mode, No Commercial) │
└──────────────────────┬───────────────────────┘
                       │ Google APIs SSL (OAuth2)
                       ▼
┌──────────────────────────────────────────────┐
│          Google Sheets Live CRM              │
│  Sheet ID: 1Inan5Laj_CsxraX0JpByJ3466QNbc... │
│  Tabs: LEADS, REVIEW_QUEUE, RESEARCH_LOG    │
└──────────────────────────────────────────────┘
```

---

## 2. Recommended Hosting Providers

Because the backend maintains search circuits, lead recovery indices, and long-running discovery routines, it requires a persistent container/VM runtime. Vercel Serverless is neither supported nor appropriate.

### Option A: Render (Recommended)

Render offers first-class support for Docker services with persistent disks, automatic SSL, and Blueprint deployment via [`render.yaml`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/render.yaml).

#### Step-by-Step Render Setup:
1. Log in to [Render](https://dashboard.render.com/).
2. Click **New +** -> **Blueprint**.
3. Connect your GitHub repository: `https://github.com/Gurpreet931977/DrippMediaLeadsMachine`.
4. Render will detect [`render.yaml`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/render.yaml) automatically and provision:
   - Web Service: `dripp-media-leads-backend`
   - Persistent Disk: `dripp-leads-data` mounted at `/app/data` (1 GB)
   - Health Check: `/api/system/health`
5. Under **Environment Variables**, provide the secret:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: Paste the entire content of your Google Service Account JSON file (`poised-eye-509816-a4-5444ff3f8520.json`).
   - `OPERATOR_KEY`: Generate or paste a secure random string (used to unlock operator controls in the frontend).
6. Click **Apply**.
7. Once deployed, Render will provide a public HTTPS URL, for example: `https://dripp-media-leads-backend.onrender.com`.

---

### Option B: Railway

1. Install Railway CLI or connect via the Railway Web Console.
2. Create a new Project from GitHub repo `Gurpreet931977/DrippMediaLeadsMachine`.
3. Add a Persistent Volume mounted at `/app/data`.
4. Set required Environment Variables in the Railway dashboard.
5. In Settings, enable **Generate Domain** to get an HTTPS endpoint (e.g. `https://dripp-leads.up.railway.app`).

---

### Option C: Self-Hosted VPS (Docker Compose + Caddy / Nginx)

For total data sovereignty on an Ubuntu/Debian VPS:
1. Clone the repository on the VPS:
   ```bash
   git clone https://github.com/Gurpreet931977/DrippMediaLeadsMachine.git /opt/dripp-leads
   cd /opt/dripp-leads
   ```
2. Create `.env` from template and copy your Google Service Account JSON file.
3. Start the container:
   ```bash
   docker-compose up -d --build
   ```
4. Point a DNS record (e.g. `api.drippmedia.com`) to your VPS IP address.
5. Configure Caddy for instant HTTPS reverse proxy:
   ```caddyfile
   api.drippmedia.com {
       reverse_proxy localhost:8000
   }
   ```

---

## 3. Required Environment Variables

Configure these on the hosted backend container/environment:

| Variable | Recommended Value | Purpose |
| :--- | :--- | :--- |
| `ALLOWED_ORIGINS` | `https://dripp-media-leads-machine.vercel.app,http://localhost:3000` | Whitelisted frontend origins for CORS |
| `OPERATOR_KEY` | Strong random passphrase (min 16 chars) | Master key to issue short-lived operator sessions |
| `GOOGLE_SHEET_NAME` | `Dripp Media Leads CRM` | Google Spreadsheet title |
| `GOOGLE_SHEET_KEY` | `1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc` | Target Google Spreadsheet ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Content of Service Account JSON | Google API OAuth2 credentials |
| `TRAVEL_MODE` | `ACTIVE` | Safety lock: blocks live commercial actions |
| `COMMERCIAL_ACTIONS_ENABLED` | `false` | Immutable safety lock |
| `AUTOMATED_EMAIL_ENABLED` | `false` | Immutable safety lock |
| `CRON` | `DISABLED` | Immutable safety lock |

---

## 4. Connecting the Vercel Frontend

Once your backend is live with an HTTPS URL:

1. Open the Vercel frontend: **`https://dripp-media-leads-machine.vercel.app/`**
2. In the sidebar, click **Settings**.
3. Under **Backend API URL**, enter your hosted backend URL:
   ```
   https://dripp-media-leads-backend.onrender.com
   ```
4. Click **Test Gateway & CRM Connectivity**:
   - The UI will independently probe:
     - **Backend Gateway**: checks latency and operational health (`/api/system/health`).
     - **Google Sheets CRM**: verifies live row count on `LEADS` and `REVIEW_QUEUE` (`/api/crm/status`).
5. Under **Operator Session & Security**:
   - Enter your `OPERATOR_KEY` and click **Authenticate Session**.
   - A short-lived (2-hour TTL) token (`dop_...`) is stored in `sessionStorage` (never in `localStorage`).
   - The badge updates to **AUTHENTICATED (2H)**.
6. The entire frontend will now display live CRM data from Google Sheets!

---

## 5. Security & Operator Key Lifecycle

1. **No Master Keys in Browser Storage**:
   The frontend automatically purges legacy keys from `localStorage` on load. Operator keys are sent once over HTTPS to `POST /api/operator/login` to obtain an ephemeral session token.
2. **Session Token Expiry**:
   Session tokens expire automatically after 2 hours. Clicking **Lock / Logout** destroys the session server-side and client-side.
3. **Protected State Modifications**:
   All state-changing endpoints (`POST`, `PUT`, `DELETE`, `PATCH` under `/api/`) reject unauthorized requests with HTTP 401. Read-only health and lead listings remain accessible to the dashboard.
4. **CORS Invariants**:
   CORS is strictly locked to approved production origins. Wildcard `*` origins are rejected for credentialed requests.

---

## 6. Disaster Recovery & Health Monitoring

- **Health Endpoint**: `GET /api/system/health`
  Returns `{"status": "OK", "canonical_health_status": "OPERATIONAL"}`.
- **CRM Endpoint**: `GET /api/crm/status`
  Returns live spreadsheet connection details, tab status, and lead count.
- **Safe Recovery**: If the backend restarts, all lead state is safely stored in Google Sheets and local persistent run logs in `/app/data`.
