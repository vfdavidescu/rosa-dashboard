# Handoff: history of Rosa Dash so far

Written at the end of the claude.ai conversation that built this project
(18 Oct 2026), so work can continue in Claude Code. `CLAUDE.md` describes the
code as it is; this file explains how it got here and what's open.

## The owner and the car

- Victor, an SRE, runs this himself (Docker on his Mac, `~/Projects/rosa-dash`).
- Car: 2025 Toyota Corolla Touring Sports hybrid (Excel trim), red, nicknamed
  **Rosa**, UK. MyToyota Europe account. Distances in miles, km shown in grey.
- Motivation: the MyToyota iOS app asks for the password on every launch and
  doesn't support password autofill.

## How we got here

1. **Approach.** Rather than reverse-engineer the app, used
   [pytoyoda](https://github.com/pytoyoda/pytoyoda) (community library behind the
   Home Assistant Toyota integration; EU only, unofficial). Installed version: 6.0.0.
2. **First run fixes.**
   - `PermissionError: '.cache'`: pytoyoda's HTTP client (hishel) writes `.cache/`
     to the working dir. Fixed in the Dockerfile by running from `/data`.
   - `TypeError: Api.get_trips() got an unexpected keyword argument 'route'`:
     pytoyoda 6.0.0 bug in its built-in `trip_history` endpoint. Worked around
     with `vehicle.update(skip=["trip_history"])`; trips are fetched separately.
   - pytoyoda logs full login bodies at DEBUG, and the account password ended up
     in pasted logs. Logging is now capped at WARNING. Victor was told to change
     his MyToyota password; confirm he has.
3. **UI refinements:** light/dark toggle, miles + km, phone layout, car picture
   (Toyota's render or `images/car.jpg`), status pill (all good / N things to
   check), odometer/fuel/range in the car card, `CAR_MODEL_NAME` override
   (Toyota calls it "Corolla HB/TS" for both body styles).
4. **Raw data review.** Toyota returns more than we use. Notable unused data:
   notifications history (included a 12 V battery low warning on 12 Oct 2026),
   light status, Toyota's own `overallStatus`, climate preset, subscriptions
   (Remote Services until June 2029).
5. **Service & Maintenance page** (`/service`): Toyota service records stored
   permanently in `maintenance.json`; tyre rotations logged by hand with a
   "next due" tracker (5,000 mi or 6 months by default).
6. **Remote Control card.** Opt-in (`REMOTE_CONTROL=true`), 3-second hold to send,
   server-side 15 s cooldown, `X-Rosa` CSRF header, audit log in `command-log.json`.
7. **File logging** to `./logs/rosa.log` and `./logs/pytoyoda.log`.

## Open issue: Unlock failed

Victor's first real remote command (Unlock) failed. Exact error not yet seen.

To get it (if not already done):

```bash
docker compose logs rosa-dash > logs/before-rebuild.txt   # before rebuilding
docker compose cp rosa-dash:/data/command-log.json logs/
docker compose up -d --build
```

Then read `logs/before-rebuild.txt` / `logs/command-log.json`, or retry a
harmless command and read `logs/rosa.log`. The error message contains Toyota's
HTTP status and response body.

**Leading hypothesis.** The vehicle profile has two capability sets that disagree:

- `extendedCapabilities` (what `app/remote.py` uses): `doorLockUnlockCapable`,
  `hazardCapable`, `hornCapable`, `climateCapable`, `trunkLockUnlockCapable`,
  `powerWindowsCapable` are all **true**.
- `remoteServiceCapabilities`: `dlockUnlockCapable`, `hazardCapable`,
  `trunkCapable`, `powerWindowCapable` are all **false**.

If Toyota rejects the command as unsupported, gate buttons on both sets (or on
whichever proves accurate) and hide what the car can't do. Other possibilities:
the car must be in a particular state, or the EU backend rejects this command
(pytoyoda issue #274 lists EU rejections with `CTP-REMOTE-40006`).

## Ideas discussed but not built

- Notifications/alerts card (would have caught the 12 V battery warning)
- Use Toyota's `overallStatus` and light status in the status pill
- Trip routes on the map
- Subscription expiry note
- Home-screen icon for the phone (currently iOS uses a screenshot)
- A `/api/raw` page showing Toyota's current raw responses
