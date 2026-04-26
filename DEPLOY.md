# Deployment to Render — step-by-step

You need to do steps **1, 2, 4, 6, 7, 8** in your browser. I've prepared everything else (`render.yaml`, `requirements.txt`, code).

---

## 1. Create an empty private GitHub repo

1. Go to https://github.com/new
2. Name: `fb-taverns-reconciliation` (anything is fine)
3. **Private**
4. Do NOT add README, .gitignore, or licence — we already have those
5. Create

Copy the repo URL (looks like `https://github.com/<you>/fb-taverns-reconciliation.git`).

---

## 2. Tell me the repo URL

Paste it back to me and I'll run the `git init` + `git push` from this folder. (I want to double-check the `.env` and `outputs/` aren't pushed before any commits leave your machine.)

---

## 3. (I do this) Initial git push

```bash
git init
git add -A
git status                # I'll verify .env and outputs/ are excluded
git commit -m "phase 2: airtable + fastapi service"
git remote add origin <your repo URL>
git branch -M main
git push -u origin main
```

---

## 4. Create the Render account

1. https://render.com → Sign up with GitHub (easiest — gives Render permission to read your repo)
2. Authorise it for the new repo (or "all repos" if you trust Render — fine for a personal account)

---

## 5. (I do this) Render service creation

In the Render dashboard:
1. **New** → **Blueprint**
2. Connect to the `fb-taverns-reconciliation` repo
3. Render reads `render.yaml` and proposes one service (`fb-taverns-reconcile`)
4. Click **Apply**
5. The first deploy starts. It'll fail at first because env vars aren't set — that's expected.

I'll walk you through this part on the call/chat.

---

## 6. Set environment variables in Render

Service → **Environment** tab → add:

| Key | Value | Notes |
|---|---|---|
| `AIRTABLE_TOKEN` | `pat6DDk…` | from your `.env` |
| `AIRTABLE_BASE_ID` | `appyDA69D2YhdpsA4` | from your `.env` |
| `WEB_USERNAME` | `admin` | already in render.yaml |
| `WEB_PASSWORD` | _pick a strong password_ | this is what you'll use to log in |
| `PYTHON_VERSION` | `3.12.7` | already in render.yaml |

Save. Render redeploys automatically.

---

## 7. Test the deployed service

After the deploy finishes (Logs tab shows `Application startup complete`):

1. Click the URL at the top of the service page (looks like `https://fb-taverns-reconcile.onrender.com`)
2. Log in with `admin` / your `WEB_PASSWORD`
3. Upload one of the LWC weekly files
4. Open Airtable — there should be a new row in **Files** and a batch of new rows in **Mismatches**

---

## 8. Add Jason as a read-only Airtable collaborator

1. Open the Airtable base
2. **Share** (top right) → **Invite by email**
3. Email: `jason.french@fbtaverns.com`
4. Role: **Read-only**
5. Invite

He gets an email with a link. He'll be able to view all five tables but not edit anything.

> **Note on the review workflow:** the `Mismatches.status` field has `open / acknowledged / resolved` for a future review workflow, but Read-only means Jason can't change them yet. To let employees acknowledge mismatches without giving them write access on `PricingRules`, we'll need to build an Airtable **Interface** (Phase 7 polish) or upgrade the base permissions. Not blocking for Phase 2.

---

## Once deployed, the regular workflow is:

- **Weekly** — open the Render URL, upload the LWC sales file, check Airtable for mismatches.
- **When pricing changes** — locally run `python reconcile.py build-master --tenant-folder <new-letters> --to-airtable --valid-from <date>`. (Cloud UI for this is Phase 7.)
- **Anything weird** — Render dashboard has logs; Airtable has the file's `raw_hash` for de-dup proof.

---

## Costs

| | Cost |
|---|---|
| Render starter web service | ~£5-7/mo |
| Airtable Free | £0 (up to 1,000 records per base — we'll exceed this in ~2 months of weekly files) |
| Airtable Team | ~£20/seat/mo (50k records, proper roles) — **upgrade when free tier breaks** |
| Anthropic API (Phase 6 only) | a few £/mo |
