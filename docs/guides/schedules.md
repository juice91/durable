# Schedules

A schedule starts a published workflow on a cron expression in a time zone, with a fixed input.

## Create one

In Studio, open the workflow and its **Schedules** tab. Through the API:

```sh
curl -X POST "$API/api/v1/workflows/$WORKFLOW_ID/schedules" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name": "Weekday digest", "cron": "0 9 * * 1-5", "timezone": "Europe/Berlin", "input": {}}'
```

`cron` has five fields. `GET /api/v1/workflows/schedules` lists every schedule;
`PATCH /api/v1/workflows/schedules/{schedule_id}` changes one or pauses it with `{"active": false}`.

## How a schedule fires

- The worker checks for due schedules once a minute by default (`EAP_SCHEDULE_TICK`).
- Each fire runs the **latest published version** at that moment.
- The input is checked against that version's `input_schema`. If it no longer fits, or the workflow is archived, no run is
  created: the schedule is stopped and says why in `last_error`.
- A missed window (the workers were down) fires **once**, not once for every minute that was missed.
- The run id is derived from the schedule and the fire time, so a crash during a tick cannot start the same fire twice.
