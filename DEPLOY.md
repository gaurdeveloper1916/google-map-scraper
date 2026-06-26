# Deploying to Render (Free Tier)

## Prerequisites
- A [Render](https://render.com) account (free)
- Your project pushed to a GitHub repository
- Your `firebase_credentials.json` file (keep it local — never commit it)

---

## Step 1 — Push to GitHub

If you haven't already:

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

> `firebase_credentials.json` is already excluded by `.gitignore` — do not force-add it.

---

## Step 2 — Get your Firebase credentials as a JSON string

Run this in your terminal to copy the credentials as a single-line JSON string:

```bash
cat firebase_credentials.json | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)))"
```

Copy the entire output — you'll paste it into Render in the next step.

---

## Step 3 — Create a Web Service on Render

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service**
2. Connect your GitHub repo
3. Render will auto-detect the `render.yaml` — confirm the settings:
   - **Runtime:** Docker
   - **Plan:** Free
4. Under **Environment Variables**, add:
   - Key: `FIREBASE_CREDENTIALS_JSON`
   - Value: paste the JSON string from Step 2
5. Click **Create Web Service**

Render will build the Docker image (takes ~5–10 min on first deploy due to Chromium download).

---

## Step 4 — Verify

Once deployed, your API will be live at:

```
https://google-maps-scraper.onrender.com
```

Open the interactive docs at:

```
https://google-maps-scraper.onrender.com/docs
```

---

## Local Development

The app still works locally with your `firebase_credentials.json` file:

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn api:app --reload
```

---

## Notes

- **Free tier caveat:** The service spins down after 15 minutes of inactivity. The first request after idle takes ~30 seconds to wake up.
- **Scraping jobs:** Long-running scrape jobs (100+ places) can take several minutes. The SSE log stream (`/scrape/{job_id}/stream`) keeps the connection alive. Render's free tier has a 30-second request timeout for HTTP but SSE streams are kept alive by the ping events in the code.
- **Upgrading:** If you need always-on service, Render's Starter plan is $7/month.
