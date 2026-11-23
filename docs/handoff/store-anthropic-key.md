# Handoff: store the Anthropic API key for Dieter

For Claude Code on Curtis's other computer, where the key is. Written 2026-11-22.

**Never display, log, echo or commit the key.**

Curtis will copy the key to the clipboard first. Store it in Google Secret Manager, project `recipes-442702`,
as a new secret named `dieter-anthropic-api-key`.

1. Check that `gcloud` is installed and signed in: `gcloud auth list` and `gcloud config get-value project`.
   If it isn't signed in, stop and tell Curtis; don't sign in for him.
2. Check whether the secret already exists:
   `gcloud secrets describe dieter-anthropic-api-key --project recipes-442702 --format="value(name)"`
3. In PowerShell, pipe the clipboard straight in so the key never appears on screen:
   - **If the secret doesn't exist:**
     `(Get-Clipboard -Raw).Trim() | gcloud secrets create dieter-anthropic-api-key --project recipes-442702 --replication-policy=automatic --data-file=-`
   - **If it already exists**, add a new version instead:
     `(Get-Clipboard -Raw).Trim() | gcloud secrets versions add dieter-anthropic-api-key --project recipes-442702 --data-file=-`
4. Check it without revealing it. This prints only the length and the first 7 characters, which should be `sk-ant-`:
   `$k = gcloud secrets versions access latest --secret dieter-anthropic-api-key --project recipes-442702; "$($k.Length) chars, starts $($k.Substring(0,7))"; Remove-Variable k`
5. Don't put the key in any `.env` file, repo, commit or chat message. Don't deploy anything; the other computer
   handles the deploy (branch `claude-brain`).
6. Report back with one line: the secret was created (or a version added), its length and its `sk-ant-` prefix.

When it's done, clear the clipboard: `Set-Clipboard -Value " "`
