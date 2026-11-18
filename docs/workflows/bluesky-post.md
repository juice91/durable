# Bluesky Post

[`bluesky-post.json`](../../examples/workflows/bluesky-post.json) is a published workflow that posts a short
text to a Bluesky (AT Protocol) account. It is also the worked example of a workflow calling a JSON API with a
credential: it uses only platform tools, [`http.request`](../tools/http-request.md), `clock.now` and
`workflow.return`; nothing in the platform is specific to Bluesky. The example needs no agent. Its file holds
`description`, `agents` (empty), `input` (a sample) and `spec`.

## Set it up

1. **Store the app password.** In **Settings → Secrets** create a secret named `bluesky-app-password` (a masked
   input; the value is write-only) or let an agent ask for it with the `secrets.request` tool. Use a Bluesky *app
   password* (Settings → App Passwords on Bluesky), not the account password. The password is never written in
   the workflow, a run input or a chat: the spec holds only the reference `secret:bluesky-app-password`.
2. **Publish the workflow.** `POST /api/v1/workflows` with `{"spec": <spec>}`, then
   `POST /api/v1/workflows/{id}/versions` (no warnings).
3. **Run it.** `POST /api/v1/workflows/{id}/runs` with `{"input": {"text": "...", "langs": ["zh-Hant"]}}`:
   - `text`: the post, 1 to 300 characters;
   - `langs`: the post's language tags (1 to 3), **required**: the workflow engine applies no input defaults, so
     a default of `["zh-Hant"]` cannot be written in the schema. Pass it every time.

The output is `{"uri": "at://…/app.bsky.feed.post/…", "cid": "…", "handle": "example-account.pds.example.social"}`.

## Use another account

The account is three constants in the spec, not inputs. To post to another account, copy the spec and change them
in the steps below:

- the handle `example-account.pds.example.social` in the step `session` (`identifier`);
- the PDS URL `https://pds.example.social` in the `url` of `session` and `post` (for an account on bsky.social the
  PDS is `https://bsky.social`);
- the secret name `bluesky-app-password` in `session` (`secret:bluesky-app-password`; create a secret with the new
  name).

Never put the password itself in the spec: a published spec is readable by everyone who can read workflows, and
the reference is all that is needed.

## The steps

`now` reads the clock (`createdAt` of the record).

<!-- example: bluesky-post.json /spec/steps/1 -->
```json
{
  "id": "session",
  "type": "tool",
  "tool_id": "http.request",
  "input": {
    "method": "POST",
    "url": "https://pds.example.social/xrpc/com.atproto.server.createSession",
    "json": {
      "identifier": "example-account.pds.example.social",
      "password": "secret:bluesky-app-password"
    },
    "drop_response_paths": [
      "/refreshJwt",
      "/email",
      "/emailConfirmed"
    ]
  },
  "next": "login_gate"
}
```

`session` logs in (`com.atproto.server.createSession`). `http.request` replaces `secret:bluesky-app-password` by
the stored value inside the call, right before the request is sent; the recorded step input, the events and the
step output show the reference only, and the answer is redacted with the value. A workflow tool step needs no
approval: the person who published the workflow reviewed the URL and the reference. (An *agent* that calls
`http.request` with a secret reference waits for a person's approval instead.)

A non-2xx answer is a result, not an error, so `login_gate` decides:

<!-- example: bluesky-post.json /spec/steps/2 -->
```json
{
  "id": "login_gate",
  "type": "branch",
  "cases": [
    {
      "when": {
        "op": "eq",
        "left": {
          "$ref": "steps.session.output.ok"
        },
        "right": true
      },
      "next": "post"
    }
  ],
  "default": "login_failed"
}
```

<!-- example: bluesky-post.json /spec/steps/3 -->
```json
{
  "id": "login_failed",
  "type": "tool",
  "tool_id": "workflow.return",
  "input": {
    "value": {
      "$template": {
        "error": {
          "code": "bluesky_auth_failed",
          "message": "Bluesky refused the login with status {{steps.session.output.status}}; check the handle and the app password secret"
        }
      }
    }
  }
}
```

A refused login (a wrong handle or password answers 401) goes to `login_failed`, which ends the run with the
stable code `bluesky_auth_failed`. `workflow.return` returns its input unchanged; a tool that returns
`{"error": {"code", "message"}}` fails the tool step, and so the run, with that code (the same convention as
`kb-source-sync`). Network failures end the run with the tool's own codes: `http_timeout`,
`http_connect_failed`, `outbound_blocked`, `secret_not_found` (the secret is not stored; nothing is sent).

<!-- example: bluesky-post.json /spec/steps/4 -->
```json
{
  "id": "post",
  "type": "tool",
  "tool_id": "http.request",
  "input": {
    "method": "POST",
    "url": "https://pds.example.social/xrpc/com.atproto.repo.createRecord",
    "headers": {
      "$template": {
        "Authorization": "Bearer {{steps.session.output.json.accessJwt}}"
      }
    },
    "json": {
      "$template": {
        "repo": "{{steps.session.output.json.did}}",
        "collection": "app.bsky.feed.post",
        "record": {
          "$type": "app.bsky.feed.post",
          "text": "{{input.text}}",
          "createdAt": "{{steps.now.output}}",
          "langs": "{{input.langs}}"
        }
      }
    }
  },
  "next": "post_gate"
}
```

`post` creates the record (`com.atproto.repo.createRecord`). The step reads two earlier outputs and one input, so
it uses `{"$template": ...}`: a JSON value whose strings may hold `{{ <ref path> }}` placeholders, resolved from
the run's data like a `$ref`. A string that is exactly one placeholder keeps the value's type (`langs` stays an
array); a string with other text around it (`Bearer {{ steps.session.output.json.accessJwt }}`) concatenates
strings, numbers and booleans. Keys are never templated.

<!-- example: bluesky-post.json /spec/steps/5 -->
```json
{
  "id": "post_gate",
  "type": "branch",
  "cases": [
    {
      "when": {
        "op": "eq",
        "left": {
          "$ref": "steps.post.output.ok"
        },
        "right": true
      },
      "next": "result"
    }
  ],
  "default": "post_failed"
}
```

<!-- example: bluesky-post.json /spec/steps/6 -->
```json
{
  "id": "post_failed",
  "type": "tool",
  "tool_id": "workflow.return",
  "input": {
    "value": {
      "$template": {
        "error": {
          "code": "bluesky_post_failed",
          "message": "Bluesky refused the post with status {{steps.post.output.status}}"
        }
      }
    }
  }
}
```

<!-- example: bluesky-post.json /spec/steps/7 -->
```json
{
  "id": "result",
  "type": "tool",
  "tool_id": "workflow.return",
  "input": {
    "value": {
      "$template": {
        "uri": "{{steps.post.output.json.uri}}",
        "cid": "{{steps.post.output.json.cid}}",
        "handle": "{{steps.session.output.json.handle}}"
      }
    }
  }
}
```

A refused post (for example an expired session or an invalid record) ends the run with `bluesky_post_failed`;
otherwise `result` shapes the output.

## Things to know

- **The access token is recorded, the refresh token is not.** `session` drops `/refreshJwt`, `/email` and
  `/emailConfirmed` from the login answer (`drop_response_paths`) before it becomes the step output, so they never
  reach the events, spans or checkpoints. `accessJwt` (valid for about two hours) is still recorded in the step
  output, because the next step needs it. The `post` step's recorded input shows the Authorization header masked.
  The password itself is never recorded. Anyone who can read the run's events can still read the access token
  while it is valid; treat run access accordingly.
- A post is not idempotent. A tool step that was started and whose outcome is unknown after a worker crash ends
  the run `needs_attention` instead of posting twice.
- Rate limits: a 429 from the PDS is a refused post (`bluesky_post_failed`); the step output keeps the status and
  the `retry-after` header.
