# Authoring workflows

Conventions for writing a workflow definition. The Architect follows them and the publish check
reports deviations.

## Step naming

Step ids follow one convention, so workflows read the same everywhere:

| Step | Id | Examples |
|---|---|---|
| `branch` | `<subject>_gate` | `check_gate`, `login_gate`, `richtext_gate` |
| `workflow.return` tool step | `ret_<reason>` | `ret_posted`, `ret_dry`, `ret_check_failed` |
| every other step | a snake_case noun or verb that says what the step does | `fetch_rss`, `pick`, `write`, `check`, `post_thumb` |

No step id is longer than 40 characters. The rules apply at every depth: loop and `foreach` bodies
included, and the branches of a `parallel` step.

Publishing a workflow checks the ids. Each id that breaks the convention produces one
`step_id_convention` warning with the suggested name, for example `gate_check` on a branch suggests
`check_gate` and `r_posted` on a return suggests `ret_posted`. It is only a warning: the version is
published, and the warning appears wherever publish warnings do (the publish response's `warnings`,
the Architect's publish card and Studio). To adopt a suggestion, rename the step and update the
`next`, `default`, `start` and `$ref` paths that name it, then publish again.
