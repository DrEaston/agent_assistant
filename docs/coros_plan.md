# COROS: Dieter's own copy of every run

Curtis runs with a COROS watch. Strava's API terms only let Dieter show his Strava data to him and bar using it for
machine learning, so new runs should come from COROS directly. His history stays as Strava brought it in.

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

1. **Switch new activities to COROS** (apps/trainer/coros.py, fit.py). Strava already brought in his history, so
   nothing old is fetched. From the moment he connects COROS (Trainer → Log), each pass lists the last two weeks and
   every activity that started after the switch is fetched as a FIT file, read, and added to his log
   (source `coros`) with its speed analysis, exactly like a Strava run. Strava stops adding new activities
   (`strava.Sync.handle`); changes to activities it brought in before still arrive. COROS has no push, so a pass runs
   when Trainer or Today opens (at most every 10 minutes) or with "Check now". Disconnecting hands new activities
   back to Strava. FIT files are kept like Media's files (`coros/<user>/<labelId>.fit`).
2. **Test with my latest run**: reads his newest COROS run without adding it and shows it next to the same run from
   Strava (distance, time, heart rate, what was fast, top speed), to check the two agree before relying on COROS.
3. Later, if useful: COROS's own numbers (threshold pace, training load, sleep, HRV) for comparison, and the
   capstone's data rules revisited for COROS-sourced runs.

## Open questions

- What `querySportRecords` returns exactly (field names, time zone of start times): step 1 stores the raw record
  so step 2 can read it properly once real answers are in.
- Whether COROS's 50-a-day limit counts per app or per person across apps (it says per platform user); a few new
  activities a day is far below it either way.
- COROS's sport codes in activity lists (the FIT file's own sport decides Run / Ride / ... in the log).
