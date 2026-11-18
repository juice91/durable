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
