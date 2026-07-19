# Cloud Run Deployment

This app can be deployed to Google Cloud Run for phone access away from home.

## Important Limits

- This is a personal deployment designed for low usage and low cost.
- `projects.db` is restored from Google Cloud Storage to `/tmp/projects.db` when Cloud Run starts.
- After SQLite commits, the app uploads a consistent database snapshot back to Cloud Storage.
- Planner tasks, scheduler agenda items, Dieter edits, and recipe changes all write to the SQLite database at runtime.
- Uploaded recipe images and generated thumbnails are restored from and synced to Cloud Storage.
- This approach is intended for one-person usage. For heavier concurrent use, move the database to Cloud SQL or another managed database.

## One-Time Setup

1. Install Google Cloud CLI:

```powershell
winget install --id Google.CloudSDK -e
```

If the installer hangs, install manually from:

```text
https://cloud.google.com/sdk/docs/install
```

2. Open a new terminal, then authenticate:

```powershell
gcloud init
gcloud auth login
gcloud auth application-default login
```

3. Create or select a Google Cloud project and make sure billing is enabled.

## Deploy

From the repo root:

```powershell
$env:GOOGLE_CLOUD_PROJECT="your-project-id"
powershell -ExecutionPolicy Bypass -File scripts\deploy_cloud_run.ps1
```

The script deploys with:

- service name: `dieter`
- region: `us-central1`
- public unauthenticated access
- minimum instances: `0` by default, or `1` when using the keep-warm option
- maximum instances: `1`
- concurrency: `1`
- memory: `1Gi`
- database path: `/tmp/projects.db`
- uploads path: `/tmp/uploads`
- Cloud Storage bucket: `<project-id>-dieter-data` by default
- Cloud Storage prefix: `dieter` by default
- OpenAI API key: Secret Manager secret `dieter-openai-api-key`
- optional account registration code: `DIETER_REGISTRATION_CODE`

If `OPENAI_API_KEY` is set in your terminal, it is passed to Cloud Run.
If it is not set in your terminal, the deploy script reads it from `.env`.
The key is stored in Secret Manager and mounted into Cloud Run as `OPENAI_API_KEY`.

For multi-user mode, set `DIETER_REGISTRATION_CODE` in your shell or `.env` before deploy.
The first account created becomes admin and claims existing single-user data.

Sharing is available through API endpoints:

- `POST /api/projects/{project_id}/share`
- `POST /api/recipes/meals/{meal_id}/share`
- `POST /api/recipes/components/{component_id}/share`

Each accepts JSON like:

```json
{"email":"friend@example.com","permission":"view"}
```

To override the bucket or object prefix:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_cloud_run.ps1 `
  -ProjectId your-project-id `
  -DataBucket your-globally-unique-bucket `
  -DataPrefix dieter
```

To keep `dieter.ai` warm and avoid Cloud Run cold starts, deploy with one
minimum instance:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_cloud_run.ps1 `
  -ProjectId your-project-id `
  -MinInstances 1
```

You can also update the live service without a full redeploy:

```powershell
gcloud run services update dieter `
  --project your-project-id `
  --region us-central1 `
  --min-instances 1
```

After scheduler or planner schema changes, no manual migration command is needed. The app runs `db.init()` on startup and adds missing SQLite tables/columns automatically.

## Cost Controls

Use Cloud Run with `--min-instances 0` when the lowest cost is more important
than startup latency. Use `--min-instances 1` for `dieter.ai` when you want the
site to stay responsive instead of going to sleep.
This SQLite + Cloud Storage persistence path intentionally uses `--max-instances 1`
and `--concurrency 1` to avoid concurrent database snapshot overwrites.
Set a Google Cloud billing budget alert before using it heavily.

## Later Database Upgrade

For heavier production use, move:

- database from SQLite snapshots to Cloud SQL or Firestore
- file serving from local restored files to direct Cloud Storage-backed URLs
