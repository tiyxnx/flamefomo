# FLAME FOMO — Vercel-ready Google-connected build

This is a **single Flask app**. Vercel currently supports Flask deployment without the old `api/` + `vercel.json` setup.

The app serves:

- `index.html` at `/`
- FLAME FOMO API at `/api/...`
- Google sign-in through Google Identity Services
- Gmail OAuth for read-only event-mail access
- Google Calendar OAuth for free/busy access

## Deploy to Vercel

Upload/push the **entire folder**. Do not upload only `index.html` or only `backend.py`.

Vercel should detect `backend.py` as the Flask app because it contains `app = Flask(__name__)`.

## Vercel environment variables

Add these in Project → Settings → Environment Variables:

### Required

`APP_SECRET`

Use a long random value.

`GOOGLE_CLIENT_ID`

Your Google OAuth **Web application** client ID.

`GOOGLE_CLIENT_SECRET`

The matching Google OAuth client secret.

### Optional

`FRONTEND_URL`

Your Vercel URL, e.g. `https://your-project.vercel.app`.

If omitted, FLAME FOMO automatically uses the current request's origin.

`DATABASE_PATH`

Defaults to `/tmp/fomo.sqlite3` on Vercel.

## Google Cloud setup

Enable:

- Google Identity Services / OAuth
- Gmail API
- Google Calendar API

For the Web OAuth client, add your deployed Vercel URL to **Authorized JavaScript origins**:

`https://YOUR-PROJECT.vercel.app`

Add this as the **Authorized redirect URI**:

`https://YOUR-PROJECT.vercel.app/api/integrations/google/callback`

If you use a custom domain, add that origin and callback URL too.

## Put the Google client ID in the frontend

Open `config.js` and replace:

`YOUR_GOOGLE_WEB_CLIENT_ID.apps.googleusercontent.com`

with the same Web OAuth client ID you put into Vercel's `GOOGLE_CLIENT_ID` variable.

Keep:

`API_URL: ""`

because the frontend and backend are now on the same domain.

## What was fixed

- Vercel Flask entrypoint/deployment structure
- Same-domain frontend + API routing
- Google OAuth callback URL generation
- Google sign-in verification restricted to `@flame.edu.in`
- Separate Gmail and Calendar OAuth token storage
- Gmail connect/disconnect
- Calendar connect/disconnect
- Google connection status
- Removed the old hard-coded Google client widget
- Removed the dead email-code frontend path from the deployment dependency chain
- Removed the old Render-specific deployment config
- Backend database initialization when the Vercel function starts

## Google permissions

FLAME FOMO requests only:

- Gmail read-only access for event discovery
- Google Calendar free/busy access for conflict checking

It does not ask for your Google password.

Google may require accounts to be added as test users while the OAuth consent screen is in testing.

## Important production limitation

Vercel's local filesystem is not persistent. The included SQLite database is therefore appropriate for testing/demo use, but not for a campus-wide production launch. A persistent hosted database should replace SQLite before real deployment.

## Local run

```bash
pip install -r requirements.txt
export APP_SECRET='change-me'
export GOOGLE_CLIENT_ID='your-client-id'
export GOOGLE_CLIENT_SECRET='your-client-secret'
python backend.py
```

Then open `http://localhost:5000`.
