# AI 說明 (AI explanation)

「AI 說明」 reads one **published** version of a workflow and writes a plain-language explanation of it, in the
language of your browser (繁體中文 when the browser language starts with `zh`, English otherwise). Use it when
the Graph tab shows step ids, expressions and branch conditions you cannot read at a glance: what the workflow
does, where it reads from, where it posts or writes, and when it does nothing.

## Use it

1. Open **Workflows → a workflow → Graph** and click **AI 說明** in the header. A panel opens on the right.
2. Click **產生說明**. The helper runs for up to 90 seconds (「正在產生…」). The panel then shows:
   - the **summary** (2 to 4 sentences);
   - **輸入**: what each input of the workflow means;
   - **結束狀態**: what each ending (each `workflow.return` step) means, for example *slot_done = 這個時段已經發過了*;
   - **步驟**: one sentence per step, with the step id.
3. The answer is stored for that version and language. Opening the panel again, or another person opening it,
   shows the stored one with the model and the time; **重新產生** writes a new one.
4. With an explanation stored, every step in the Graph tab's **Steps** list shows its note under a small
   **AI 註解** label, and the run page's step panel shows the same note for the version that run used.

A draft has no explanation: the panel says 「發佈後可產生說明」. Publish first.

## What it does not do

- It **describes** the published version; it never changes the workflow, its draft, its schedules or a run.
- It is not a check. The helper can misread a step. Treat a note as a reading aid and open the step's definition
  when it matters. It is told to say nothing about what the definition does not show.
- It does not run the workflow, call its tools or read run data. Each tool step contributes only the tool's title
  and one-line description; each agent step only the agent's name and the first 300 characters of its
  instructions; secrets appear by their `secret:` name only, and credentials are redacted before the helper sees
  the spec.
- It does not update itself when you publish a new version: a new version has no explanation until you generate one.
- A model must be allowed for the platform (the helper uses the same ranked default model as the Architect);
  otherwise the panel shows 「No model is available…」.

## API

| Call | What it does |
|---|---|
| `GET /api/v1/workflows/{id}/explanation?version=<n>&locale=zh-TW` | The stored explanation (version defaults to the latest). 404 `explanation_missing` when none is stored, 404 `version_not_found`. |
| `POST /api/v1/workflows/{id}/explanation` `{version?, locale?: "zh-TW"\|"en", refresh?}` | Runs the helper (up to 90 s) and stores the answer; with a stored one and `refresh` false it returns that one without running. 409 `no_model`, 422 `explanation_invalid`, 502 `explanation_failed`, 504 `explanation_timeout`. |

The answer: `{workflow_id, version, locale, model, created_at, summary, inputs: [{name, meaning}],
outcomes: [{status, meaning}], steps: [{id, note}]}`. The summary is cut at 600 characters, each note at 160, and a
note for a step id the version does not have is dropped. The helper is the hidden built-in agent
`workflow-explainer` (「工作流說明員」), created on first use; it does not show in the agent list unless
`include_system` is set. Storage: table `workflow_explanations`, one row per tenant, workflow, version and locale
(migration `0034_workflow_explanations`).
