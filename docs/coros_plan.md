# COROS: Dieter's own copy of every run

Curtis runs with a COROS watch. Strava's API terms only let Dieter show his Strava data to him and bar using it for
machine learning, so Trainer should read runs from COROS directly and keep its own record.

## How Dieter gets the data

COROS runs an official MCP server for third-party apps: self-service, no application to COROS, OAuth 2.0, and
every connection sees only the athlete who signed in ([Build on COROS MCP](https://support.coros.com/hc/en-us/articles/53181619102996)).
Dieter connects to it as an ordinary OAuth client:

- Discovery: `https://mcpus.coros.com/.well-known/oauth-protected-resource` names the authorization server; its
  metadata has the authorize, token and registration endpoints (PKCE S256; scopes `openid mcp.tools offline_access`).
- Dieter registers itself per callback address (dynamic client registration, no secret), sends him to COROS to
  sign in, and keeps a refresh token.
- Tools used: `querySportRecords` (activities by date; no paging, so a month at a time) and
  `downloadActivityFitFiles` (each activity's FIT file: every second of the run, GPS included). COROS allows 50 FIT
  files per person per 24 hours, so the history arrives over several days, runs and newest first.

The larger COROS Partner API (webhooks, multi-user) needs an application and isn't needed for one athlete.

## Steps

1. **Connect and collect** (done, apps/trainer/coros.py): Connect COROS on the Log page; activities are listed into
   `tr_coros_activities`; FIT files are saved like Media's files (a local folder plus Cloud Storage under
   `coros/<user>/<labelId>.fit`). "Fetch more now" runs a pass; each pass fetches up to 8 files.
2. **Read the FIT files into Trainer**: parse each FIT file (time, distance, speed, heart rate, cadence, GPS) into
   the same per-second series the speed analysis already uses, match each COROS run to the run in Dieter's log
   (same start within a couple of minutes), and prefer COROS's series. Runs that only exist in COROS join the log.
3. **COROS first**: fetch new runs automatically (a pass when Trainer or Today opens), so Strava becomes optional.
   The share card, landing page and any write-up can then use COROS data; the capstone's data rules are revisited.
4. Later, if useful: COROS's own numbers (threshold pace, training load, sleep, HRV) for comparison.

## Open questions

- What `querySportRecords` returns exactly (field names, time zone of start times): step 1 stores the raw record
  so step 2 can read it properly once real answers are in.
- Whether COROS's 50-a-day limit counts per app or per person across apps (it says per platform user).
