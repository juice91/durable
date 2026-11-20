# Step output size

Every workflow step's output is recorded with the run, and it is capped at 32 KiB
(`EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES`). A step whose output is larger fails the run with `step_output_too_large`;
nothing is cut silently.

The usual cause is a step that records a whole HTTP response: a Bluesky `getAuthorFeed` with 30 posts is 42 to
54 KB of JSON, and an article or listing page is 140 to 280 KB of HTML. Do not record those. Use a reducer on the
[`http.request`](../tools/http-request.md#reducers) step (`response_query`, `extract_html` or `parse_feed`): the
body is reduced inside the tool, and only the reduced result becomes the step output.

For data that is already a step output, shape it with [`json.query`](../tools/json-query.md),
[`feed.parse`](../tools/feed-parse.md) or [`html.extract`](../tools/html-extract.md) in a following step, and read
only that step's output later.
