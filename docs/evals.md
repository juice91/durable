# Eval evaluators: the `regex` rule

The deterministic evaluators of a suite (and of a reusable scorer) are listed in
`apps/worker/src/eap_worker/evaluators.py`. This page covers `regex`, which is the one for
"must not contain" rules. Use it instead of an `llm_judge` for those rules: it is fast and always
gives the same answer.

## `regex`

| Field     | Default | Meaning                                                                                  |
|-----------|---------|------------------------------------------------------------------------------------------|
| `pattern` | required | RE2 syntax (no backreferences or lookarounds), at most 200 characters. An invalid pattern is refused with a 422 when the suite or scorer is saved. |
| `negate`  | `false` | `true`: the check passes when the pattern does **not** match.                            |
| `field`   | none    | A dotted path into the structured output, with the same grammar as `field_equals.path`. |
| `name`    | the kind | The name shown in results.                                                              |

**What the pattern runs over.**

- Without `field`: the output string, or the canonical JSON text of a structured output (keys
  sorted, no spaces). This is the same text as before the two options existed, so existing suites
  behave as they did.
- With `field`: only the string at that path (a digit segment indexes a list, for example
  `posts.0.text`). A text output that holds JSON is read as JSON first, as `field_equals` does. If
  the path does not exist, or the value there is not a string, the check **fails** with the reason
  `the output has no field at <path>` or `the field at <path> is not a string`. This holds for
  `negate` too: a rule that cannot be read is not reported as satisfied.

**Result reasons.** A failed check says `pattern did not match`, or, with `negate`,
`pattern matched but must not`. A reusable scorer records the fixed reasons
`the output does not match the pattern` and `the output matches a pattern it must not match`.

### Example: a posting agent

An agent returns `{"post": "...", "link": "https://..."}`. These suite evaluators state what the
post must not contain, without tripping on the `link` metadata:

```yaml
evaluators:
  - Regex: {pattern: 'https?://', negate: true, field: post, evaluation_name: no_url}
  - Regex: {pattern: '[网车这为们说]', negate: true, field: post, evaluation_name: no_simplified}
  - Regex: {pattern: '傳聞|爆料|疑似|據傳', negate: true, field: post, evaluation_name: no_rumour_words}
```

RE2 has no "traditional characters only" class, so the simplified-Chinese rule lists the characters
the post must not use.

The same options are in the Studio evaluator editor (**Must not match** and **Field path**) and in
the reusable scorer form. In an exported `pydantic_evals` file they are the `negate` and `field`
arguments of `Regex`.

# Suggested rules

After an agent or a workflow exists, Studio can propose rules for it: **Suggest rules** on the
agent's Tests tab and in its Scorers section (Config), and on the workflow's Tests tab. Nothing is
added until someone clicks.

## What a suggestion is

`POST /api/v1/agents/{id}/scorer-suggestions` and `POST /api/v1/workflows/{id}/scorer-suggestions`
(body optional: `{"max": 1..12, "include_runs": true}`, defaults 8 and true) run the built-in
**Scorer suggester** agent once and answer:

```json
{
  "suggestions": [
    {
      "title": "No refund promises",
      "rationale": "The instructions forbid promising refunds.",
      "scope": "both",
      "spec": {"kind": "regex", "pattern": "(?i)refund", "negate": true},
      "evidence": "instructions, line 2"
    }
  ],
  "model": "anthropic:claude-sonnet-5-5",
  "dropped": 1
}
```

- `spec` is a valid suite evaluator (`eap_schemas.evals`). For `scope` `scorer` or `both` it is also a
  valid reusable scorer (`contains`, `regex`, `schema_valid`, `field_equals`, `max_cost`), so it can be
  sent to `POST /scorers` as it is. `evaluator` scope means suite only (`max_duration`, `llm_judge`).
- The suggester prefers deterministic kinds and proposes at most 2 `llm_judge` rules. A judge rule gets
  the platform's default model; the suggester never picks one.
- A workflow has no scorers, so every suggestion for a workflow is `evaluator` scope.
- `dropped` counts the answer's entries that were discarded: not a valid evaluator, a regex RE2 does not
  compile, a repeated spec, or a third judge. Entries cut by `max` are not counted.
- The answer is cached for 10 minutes per tenant, target, definition (the draft, as edited) and
  `max`/`include_runs`, so a repeat click starts no run. Editing the definition asks again.
- Errors: 404 (not found), 409 `no_model` (no model is allowed for the tenant's built-in agents),
  422 (a bad body, or `suggest_invalid` when the model's answer is not the expected shape), 502
  `suggest_failed`, 504 `suggest_timeout` (the run is awaited for at most 60 seconds).

The suggester is a normal built-in agent, seeded for a tenant on first use with the Architect's ranked
default model (the same rule as the drafters) and hidden from the agent list unless `include_system` is
set. `PUT /architect/model` changes its model with the others once it exists.

## What it reads, and what it never sees

It reads, as one prompt of at most 20,000 characters:

- the target's definition: the agent's name, instructions, output schema and tool ids, or the
  workflow's name, description, input and output schemas and steps (the draft, as shown in Studio);
- with `include_runs` (the default), the inputs and outputs of up to 5 of the target's most recent
  completed runs, each cut to 600 and 800 characters.

Everything it reads passes through the platform's log redaction first (`eap_observability.redact`):
bearer and basic tokens, `sk-` keys, platform API keys, `user:password@` in URLs, and values of
`api_key`, `token`, `secret`, `password` and similar keys are replaced by `[REDACTED]`. That catches
credentials of those shapes; it cannot recognise personal data or other sensitive text a run's output
happens to hold. Turn `include_runs` off when run outputs are sensitive.

It never sees: secrets and connection credentials (it has no tools and no access to `/secrets`), memory,
knowledge-base content, attachments, other agents' or workflows' definitions, other tenants' data, or
run events beyond the final input and output. Text inside the definition or the runs is treated as
material to analyse, never as instructions. It can propose a rule only; it creates nothing. Studio's
**Add as scorer** (a reusable scorer named from the title, attached to the agent at 100% sampling),
**Add to suite** (appends the evaluator to a chosen suite, or creates "<name> rules") and the MCP tools
`scorer.create`, `scorer.attach` and `eval.suite.create` are the explicit actions that add a rule.

Limits to keep in mind: a rule is only as good as the instructions and the few runs behind it, so read
the `evidence` line before adding one; a new scorer samples 100% of an agent's runs (change the rate on
the agent page); an agent holds at most 10 scorers and a suite at most 10 evaluators.

Over MCP: `scorer.suggest` (needs the `manage` scope, because it runs a model), then `scorer.create` and
`scorer.attach`, or `eval.suite.create` with the suggestion's `spec` in `evaluators`; see
[mcp-control.md](mcp-control.md).

# Test suites from the Architect

The Architect can build and run a test suite for a workflow or agent, so a person can ask 「create test
suite for 小天娛樂新聞貼文」 in the chat. A **test suite** holds **cases** (the inputs to start the workflow
with, optionally an expected output) and **scorers** (the checks above, applied to each case's output). A
**run** executes every case against a published version and reports pass or fail per case. The Architect
offers one after it publishes a workflow, or when the person asks for tests.

Tools (the same suites, cases and runs as Studio and the [MCP tools](mcp-control.md); nothing new is stored):

| Tool | Approval | What it does |
| --- | --- | --- |
| `architect.list_eval_suites {target, id}` | no | the suites of a workflow or agent, with scorers and case counts |
| `architect.get_eval_run {run_id}` | no | a run's status, pass rate and per-case results |
| `architect.create_eval_suite {target, id, name, description?}` | yes | an empty suite; `target` is `workflow` or `agent`, `id` its slug or uuid |
| `architect.add_eval_case {suite_id, cases: [{name, inputs, expected?, metadata?}]}` | yes | up to 20 cases with one approval (`name`, `inputs` alone adds one); `inputs` must fit the workflow's input schema |
| `architect.attach_scorer {suite_id, scorer}` | yes | one scorer: `regex`, `contains`, `equals_expected`, `field_equals`, `schema_valid`, `max_duration`, `max_cost` or `llm_judge`, validated as the API does |
| `architect.start_eval_run {suite_id, version?}` | yes | queues a run of the latest (or the given) published version; it uses model calls |

Each approval card says what will happen, for example "Create test suite 'Dry runs' for workflow 'Daily
note'." A suite runs only a published version, so publish first.

## The pattern for a posting workflow

For a workflow that posts, test the path up to the post, not the post:

1. **Cases** with `inputs` such as `{"dry": true, "ignore_posted": true}`, one per source or topic.
   `dry: true` stops before the posting step; `ignore_posted: true` exercises the writer path whatever the
   time slot or what was posted already.
2. **`schema_valid`** on the returned value: its `status` is one of the allowed values and the `text` has the
   expected length.
3. **`regex`** scorers with `negate: true` and `field: "text"` that forbid what a post must not contain:
   links (`https?://`), hashtags (`#\S`), Simplified Chinese characters and rumour words.
4. **`max_duration`**, so a run that hangs fails the case.

Then start a run and read it with `architect.get_eval_run`; each failing case lists the assertion that failed.
