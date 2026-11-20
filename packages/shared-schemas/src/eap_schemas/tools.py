"""Shared, execution-free declarations for platform tools."""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _without_titles(value: Any, *, properties_map: bool = False) -> Any:
    """Remove Pydantic's presentation-only titles from a JSON Schema tree."""
    if isinstance(value, dict):
        return {
            key: _without_titles(item, properties_map=key == "properties")
            for key, item in value.items()
            if properties_map or key != "title"
        }
    if isinstance(value, list):
        return [_without_titles(item) for item in value]
    return value


class ToolDeclaration(BaseModel):
    """Portable tool metadata and its validated input contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    side_effect: bool
    external_idempotent: bool
    input_model: type[BaseModel]
    # One short example of the arguments, shown to the Architect next to the input schema.
    example: dict[str, Any] | None = None
    # What the tool returns: one example result and, where cheap, its JSON Schema. A step's output is exactly this
    # value (`steps.<id>.output`), so a workflow reads a field as `steps.<id>.output.<field>`.
    result_example: Any = None
    result_schema: dict[str, Any] | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Use the same provider-independent ID contract as AgentSpec."""
        if not _TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError("tool id must match ^[a-z][a-z0-9_.-]{0,63}$")
        return value

    def input_json_schema(self) -> dict[str, Any]:
        """Return the tool input as provider-neutral JSON Schema."""
        schema = _without_titles(self.input_model.model_json_schema())
        schema.pop("description", None)
        return schema

    def matches_implementation_schema(self, implementation_schema: dict[str, Any]) -> bool:
        """True when a function's parameter schema is this declaration's input schema (presentation titles, which
        Pydantic AI keeps on nested models, do not count)."""
        return _without_titles(implementation_schema) == self.input_json_schema()


class ClockNowInput(BaseModel):
    """Input for ``clock.now``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TicketsCreateInput(BaseModel):
    """Input for ``tickets.create``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    body: str


class SecretsRequestInput(BaseModel):
    """Input for ``secrets.request``: the secret's name (the Secrets store's name pattern) and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    purpose: str


HTTP_REQUEST_TOOL_ID = "http.request"
HTTP_MAX_RESPONSE_BYTES_CAP = 262144
HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP = 1_048_576
HTTP_BODY_SOURCE_DEFAULT_BYTES = 1_000_000
HTTP_BODY_SOURCE_MAX_BYTES = 5_000_000


class HttpBodySource(BaseModel):
    """``http.request``'s ``body_from_url``: the bytes to send are fetched first from an https image URL whose
    host is in ``allowed_hosts`` (at most ``max_bytes``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    max_bytes: int = Field(default=HTTP_BODY_SOURCE_DEFAULT_BYTES, ge=1, le=HTTP_BODY_SOURCE_MAX_BYTES)
    allowed_hosts: Annotated[list[Annotated[str, Field(min_length=1, max_length=253)]], Field(min_length=1, max_length=20)]


class HttpResponseQuery(BaseModel):
    """``http.request``'s ``response_query``: a JMESPath expression (as ``json.query``) over the JSON body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expression: Annotated[str, Field(max_length=500)]


class HttpExtractHtml(BaseModel):
    """``http.request``'s ``extract_html``: ``html.extract`` over the text body (``base_url`` defaults to the
    request URL)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["article", "links"]
    base_url: str | None = None
    max_graphemes: Annotated[int, Field(ge=1, le=20000)] = 4000
    root_selector: Annotated[str, Field(max_length=300)] | None = None
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    allowed_hosts: Annotated[list[Annotated[str, Field(min_length=1, max_length=253)]], Field(max_length=20)] | None = None
    limit: Annotated[int, Field(ge=1, le=60)] = 20
    exclude_urls: Annotated[list[str], Field(max_length=200)] | None = None
    exclude_terms: Annotated[list[str], Field(max_length=50)] | None = None


class HttpParseFeed(BaseModel):
    """``http.request``'s ``parse_feed``: ``feed.parse`` over the text body (one document); ``label`` names its
    source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_items: Annotated[int, Field(ge=1, le=50)] = 20
    max_age_hours: Annotated[float, Field(gt=0)] | None = None
    include_terms: Annotated[list[str], Field(max_length=200)] | None = None
    exclude_terms: Annotated[list[str], Field(max_length=200)] | None = None
    exclude_urls: Annotated[list[str], Field(max_length=200)] | None = None
    label: Annotated[str, Field(min_length=1, max_length=100)] | None = None


with warnings.catch_warnings():
    # The tool's argument is named `json` (what a caller writes); it shadows BaseModel.json, which
    # nothing here uses.
    warnings.simplefilter("ignore", UserWarning)

    class HttpRequestInput(BaseModel):
        """Input for ``http.request``: one HTTPS request. `json` xor `body` (`body_conflict` otherwise; the
        tool checks it). `https` only (`url_not_https`, checked by the tool). Any string equal to
        ``secret:<name>`` (a header value may contain one) is the tenant's stored secret at send time."""

        model_config = ConfigDict(extra="forbid", frozen=True)

        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
        url: str
        headers: dict[str, str] | None = None
        query: dict[str, str] | None = None
        json: Any = None
        body: str | None = None
        timeout_s: int = Field(default=20, ge=1, le=30)
        max_response_bytes: int = Field(default=65536, ge=1, le=HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP)
        drop_response_paths: list[Annotated[str, Field(pattern=r"^/")]] | None = Field(default=None, max_length=16)
        body_from_url: HttpBodySource | None = None
        response_query: HttpResponseQuery | None = None
        extract_html: HttpExtractHtml | None = None
        parse_feed: HttpParseFeed | None = None

        @model_validator(mode="after")
        def _cap_needs_a_reducer(self) -> "HttpRequestInput":
            if self.max_response_bytes > HTTP_MAX_RESPONSE_BYTES_CAP and not (self.response_query or self.extract_html or self.parse_feed):
                raise ValueError("max_response_bytes above 262144 needs a reducer")
            return self


class WorkflowReturnInput(BaseModel):
    """Input for ``workflow.return``: any JSON value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Any


FEED_PARSE_TOOL_ID = "feed.parse"
FEED_TEXT_MAX_CHARS = 1048576


class FeedInput(BaseModel):
    """One feed of ``feed.parse``'s ``feeds``: its document, a label that becomes each item's ``source``, and an
    optional any-of term filter for this feed alone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: Annotated[str, Field(max_length=FEED_TEXT_MAX_CHARS)]
    label: Annotated[str, Field(min_length=1, max_length=100)]
    include_terms: Annotated[list[str], Field(max_length=200)] | None = None


class FeedParseInput(BaseModel):
    """Input for ``feed.parse``: one RSS 2.0 or Atom document (`text`) or one to six labelled ones (`feeds`; exactly
    one of the two, `feed_args_invalid` otherwise) and the filters to apply. Pure: no I/O."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: Annotated[str, Field(max_length=FEED_TEXT_MAX_CHARS)] | None = None
    feeds: Annotated[list[FeedInput], Field(min_length=1, max_length=6)] | None = None
    max_items: Annotated[int, Field(ge=1, le=50)] = 20
    max_age_hours: Annotated[float, Field(gt=0)] | None = None
    include_terms: Annotated[list[str], Field(max_length=200)] | None = None
    exclude_urls: Annotated[list[str], Field(max_length=200)] | None = None
    exclude_terms: Annotated[list[str], Field(max_length=200)] | None = None
    now: str | None = None


JSON_QUERY_TOOL_ID = "json.query"
JSON_QUERY_DATA_MAX_BYTES = 1048576
JSON_QUERY_RESULT_MAX_BYTES = 262144


class JsonQueryInput(BaseModel):
    """Input for ``json.query``: any JSON value (at most 1 MiB serialised) and a JMESPath expression. Pure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data: Any
    expression: Annotated[str, Field(max_length=500)]

    @field_validator("data")
    @classmethod
    def _data_fits(cls, value: Any) -> Any:
        if len(json.dumps(value, ensure_ascii=False, default=str).encode()) > JSON_QUERY_DATA_MAX_BYTES:
            raise ValueError("data is larger than 1 MiB")
        return value


TEXT_CHECK_TOOL_ID = "text.check"


class TextCheckRules(BaseModel):
    """The checks of ``text.check``; every one is off unless set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_graphemes: Annotated[int, Field(ge=0, le=100000)] | None = None
    max_graphemes: Annotated[int, Field(ge=1, le=100000)] | None = None
    forbid_urls: bool = False
    forbid_hashtags: bool = False
    forbid_emoji: bool = False
    forbid_simplified_chinese: bool = False
    forbid_terms: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=200)] = []
    rumour_terms: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=200)] = []
    attribution_terms: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=200)] = []
    forbid_terms_ci: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=200)] = []
    min_han_ratio: Annotated[float, Field(ge=0, le=1)] | None = None
    max_verbatim_run: Annotated[int, Field(ge=1, le=100000)] | None = None


class TextCheckInput(BaseModel):
    """Input for ``text.check``: a text, optionally the source it must not copy, and the rules. Pure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: Annotated[str, Field(max_length=20000)]
    source: Annotated[str, Field(max_length=100000)] | None = None
    rules: TextCheckRules


HTML_EXTRACT_TOOL_ID = "html.extract"
HTML_EXTRACT_MAX_CHARS = 2 * 1024 * 1024
HTML_EXTRACT_MAX_LINKS = 60


class HtmlExtractInput(BaseModel):
    """Input for ``html.extract``: an HTML page (at most 2 MiB), the https URL it was fetched from (relative links
    are resolved against it) and a mode: ``article`` (one article's readable text and metadata) or ``links`` (the
    anchors of a listing page that match ``include_patterns``). Pure: no I/O. The size cap is the tool's own
    result (``html_too_large``), not a validation error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    html: str
    base_url: str
    mode: Literal["article", "links"]
    max_graphemes: Annotated[int, Field(ge=1, le=20000)] = 4000
    root_selector: Annotated[str, Field(max_length=300)] | None = None
    include_patterns: list[str] | None = None  # at most 10 of 200 characters: the tool answers invalid_pattern
    exclude_patterns: list[str] | None = None
    allowed_hosts: Annotated[list[Annotated[str, Field(min_length=1, max_length=253)]], Field(max_length=20)] | None = None
    limit: Annotated[int, Field(ge=1, le=HTML_EXTRACT_MAX_LINKS)] = 20
    exclude_urls: Annotated[list[str], Field(max_length=200)] | None = None
    exclude_terms: Annotated[list[str], Field(max_length=50)] | None = None


class KbSourceGetInput(BaseModel):
    """Input for ``kb.source.get``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str


class SettingsConnectionCheckInput(BaseModel):
    """Input for ``settings.connection.check``; with `require_ok` a check that is not ok is returned as
    `{error: {code, message}}` (the check's error code), which fails a workflow tool step with that code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str
    require_ok: bool = False


class KbSourceSyncInput(BaseModel):
    """Input for ``kb.source.sync``. `part_numbers` only in a targeted sync (at most 100, each 1..128 characters of
    ``[A-Za-z0-9._/-]``; the launch checks them and answers a stable code)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    mode: Literal["targeted", "incremental", "full"]
    part_numbers: list[str] = []
    restart: bool = False  # incremental / full: a new walk instead of resuming the stored cursor


class WorkflowRunWaitInput(BaseModel):
    """Input for ``workflow.run.wait``: wait up to `wait_seconds` (1..300) for a run to end."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    wait_seconds: int = Field(default=30, ge=1, le=300)


class WorkflowRunResultInput(BaseModel):
    """Input for ``workflow.run.result``: the bounded report of a sync run; with `require_success` a failed,
    cancelled or attention-needing run is a stable error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    require_success: bool = False


TOOL_DECLARATIONS: dict[str, ToolDeclaration] = {
    "clock.now": ToolDeclaration(
        id="clock.now",
        title="Current time",
        description="Returns current UTC ISO time",
        side_effect=False,
        external_idempotent=False,
        input_model=ClockNowInput,
        result_example="2026-11-18T09:30:00+00:00",
        result_schema={"type": "string", "format": "date-time"},
    ),
    "tickets.create": ToolDeclaration(
        id="tickets.create",
        title="Create ticket",
        description="Create a new ticket",
        side_effect=True,
        external_idempotent=True,
        input_model=TicketsCreateInput,
        result_example={"ticket_id": "T-123", "created": True},
        result_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}, "created": {"type": "boolean"}}},
    ),
    "secrets.request": ToolDeclaration(
        id="secrets.request",
        title="Request a secret",
        description=(
            "Ask the person for a credential. They type it into a masked card; you receive only a "
            "reference (secret:<name>) to pass as an argument, never the value"
        ),
        side_effect=True,
        external_idempotent=False,
        input_model=SecretsRequestInput,
        result_example={"ref": "secret:api-token", "stored": True},
        result_schema={"type": "object", "properties": {"ref": {"type": "string"}, "stored": {"type": "boolean"}}},
    ),
    "kb.source.get": ToolDeclaration(
        id="kb.source.get",
        title="Get a knowledge-base source",
        description=(
            "Read one knowledge-base source of the tenant: its knowledge base, connection, kind, provider, "
            "name, enabled flag, non-secret config, last successful sync and a summary of its checkpoint"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=KbSourceGetInput,
        result_example={"id": "…", "knowledge_base_id": "…", "connection_id": "…", "kind": "…", "provider": "…", "name": "Docs", "enabled": True, "config": {}, "last_successful_sync_at": "2026-11-18T09:00:00+00:00", "last_checkpoint_summary": {"segments": 3, "has_cursor": False}, "last_source_revision": None, "created_at": "…", "updated_at": "…"},
    ),
    "settings.connection.check": ToolDeclaration(
        id="settings.connection.check",
        title="Check a connection",
        description=(
            "Check that a tenant connection works (its stored credentials are accepted by the provider). "
            "Returns ok, the time of the check and a stable error code; never a credential"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=SettingsConnectionCheckInput,
        result_example={"ok": True, "checked_at": "2026-11-18T09:30:00+00:00", "error_code": None},
        result_schema={"type": "object", "properties": {"ok": {"type": "boolean"}, "checked_at": {"type": "string"}, "error_code": {"type": ["string", "null"]}}},
    ),
    "kb.source.sync": ToolDeclaration(
        id="kb.source.sync",
        title="Start a knowledge-base source sync",
        description=(
            "Start a targeted (with part_numbers), incremental or full synchronisation of a knowledge-base "
            "source. Returns the run id of the sync; use workflow.run.wait and workflow.run.result to follow it"
        ),
        side_effect=True,
        external_idempotent=True,
        input_model=KbSourceSyncInput,
        result_example={"run_id": "…", "status": "queued"},
        result_schema={"type": "object", "properties": {"run_id": {"type": "string"}, "status": {"type": "string"}}},
    ),
    "workflow.run.wait": ToolDeclaration(
        id="workflow.run.wait",
        title="Wait for a run",
        description=(
            "Wait up to wait_seconds (1 to 300) for a run of the tenant to end. Returns its status, when it "
            "last changed and whether it has ended"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=WorkflowRunWaitInput,
        result_example={"status": "completed", "updated_at": "2026-11-18T09:31:00+00:00", "terminal": True},
        result_schema={"type": "object", "properties": {"status": {"type": "string"}, "updated_at": {"type": ["string", "null"]}, "terminal": {"type": "boolean"}}},
    ),
    "workflow.run.result": ToolDeclaration(
        id="workflow.run.result",
        title="Get a sync run's report",
        description=(
            "The bounded report of a knowledge-base source sync run: counts, checkpoint and up to 50 errors. "
            "With require_success a failed, cancelled or attention-needing run is returned as an error"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=WorkflowRunResultInput,
        result_example={"run_id": "…", "status": "completed", "source_id": "…", "mode": "incremental", "discovered": 10, "inserted": 2, "updated": 1, "unchanged": 7, "documents_queued": 3, "failed": 0, "checkpoint": {}, "errors": []},
    ),
    "http.request": ToolDeclaration(
        id="http.request",
        title="HTTP request",
        description=(
            "Send one HTTPS request (GET, POST, PUT, PATCH or DELETE) with optional headers, query and a JSON or "
            "text body, and return {status, ok, headers, json or text, elapsed_ms}. A non-2xx status is a result, "
            "not an error. Put a credential as secret:<name>; it is replaced at send time and never shown. "
            "A JSON answer is returned as json, anything else (RSS, HTML) as text. A transport failure is {error: {code}} "
            "(http_timeout, http_connect_failed, outbound_blocked, secret_not_found). drop_response_paths removes "
            "JSON pointers from the answer before it is recorded. query is an object of strings. "
            "Private and metadata addresses are refused and redirects are not followed. body_from_url "
            "{url, max_bytes (default 1000000, at most 5000000), allowed_hosts} sends bytes fetched from another "
            "https URL as the body (for a link-card thumbnail: the og_image of html.extract to "
            "com.atproto.repo.uploadBlob): that URL's host must be in allowed_hosts, it must answer an image/* type "
            "within max_bytes, else {error: {code: body_source_rejected, detail}}; Content-Type is the fetched one "
            "unless you set it; do not combine with json or body. "
            "REDUCERS: a workflow step's output is capped at 32 KiB, and a Bluesky feed or a web page is larger, "
            "so give at most one reducer and only its result is recorded (the raw body never is; max_response_bytes "
            "may then go up to 1048576, else 262144): "
            "response_query {expression} runs a JMESPath expression (as json.query) over the JSON body and returns "
            "it as json, e.g. a getAuthorFeed without reposts: {expression: \"feed[?!reason].post.{uri: uri, text: record.text}\"}; "
            "extract_html {mode article|links, base_url (default the request url), max_graphemes, root_selector, "
            "include_patterns, exclude_patterns, allowed_hosts, limit, exclude_urls, exclude_terms} runs html.extract over the text and returns it "
            "as text, e.g. {mode: \"article\", max_graphemes: 3000}; "
            "parse_feed {max_items, max_age_hours, include_terms, exclude_terms, exclude_urls, label} runs feed.parse "
            "over the text, e.g. {max_items: 10, exclude_urls: [\"https://example.com/already-posted\"]}. "
            "The result keeps status, ok, headers and elapsed_ms and adds reduced {kind, input_bytes}. A failed "
            "reduction is that tool's own {error: {code}} with request {status} alongside; two reducers are "
            "{error: {code: reducer_conflict}}"
        ),
        side_effect=True,
        external_idempotent=False,
        input_model=HttpRequestInput,
        result_example={"status": 200, "ok": True, "headers": {"content-type": "application/json"}, "json": {"id": "abc"}, "elapsed_ms": 120},
        example={
            "method": "POST", "url": "https://api.example.com/v1/items",
            "headers": {"Authorization": "Bearer secret:api-token"}, "json": {"name": "x"},
            "drop_response_paths": ["/refreshToken"],
        },
    ),
    "workflow.return": ToolDeclaration(
        id="workflow.return",
        title="Return a value",
        description=(
            "Return the given value unchanged. As a workflow's last step it shapes the run's output; with a value "
            "{error: {code, message}} it ends the run with that stable error code"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=WorkflowReturnInput,
        result_example={"ended": "nothing new"},
        example={"value": {"ended": "nothing new"}},
    ),
    "feed.parse": ToolDeclaration(
        id="feed.parse",
        title="Parse a feed",
        description=(
            "Parse RSS 2.0 or Atom into numbered items {id c1.., title, link, summary (plain text, at most 400 "
            "characters), published_at, source}, newest first, plus a one-line-per-item listing, the count and what "
            "was dropped. Give one document as text (up to 1 MiB) or one to six as feeds [{text, label, "
            "include_terms}], merged and numbered once; each item's source is its feed's label. Filters: max_items "
            "(1-50, default 20), max_age_hours, include_terms (an item needs one), exclude_urls, exclude_terms "
            "(all case-insensitive, over title and summary). Pure: it fetches nothing. An unreadable or "
            "unsupported document is the error feed_invalid; a bad choice of text and feeds is feed_args_invalid"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=FeedParseInput,
        result_example={"items": [{"id": "c1", "title": "…", "link": "https://example.com/a", "summary": "…", "published_at": "2026-11-18T08:00:00Z", "source": "Source A"}], "listing": "c1｜…｜…", "count": 1, "dropped": {"excluded_url": 0, "old": 0, "no_term": 0, "term": 0, "malformed": 0}},
        example={
            "feeds": [{"text": "<the text of an http.request GET>", "label": "Source A", "include_terms": ["keyword"]}],
            "max_age_hours": 24, "exclude_urls": ["https://example.com/already-posted"], "exclude_terms": ["term to skip"],
            "max_items": 10,
        },
    ),
    "json.query": ToolDeclaration(
        id="json.query",
        title="Query JSON",
        description=(
            "Run a JMESPath expression (at most 500 characters) over any JSON data (at most 1 MiB) and return "
            "{result}; a missing path is a null result. Projections, filters, pipes and multiselect hashes work: "
            "items[?id=='c3'] | [0], {links: feed[].uri}. Pure. Errors: expression_invalid, result_too_large "
            "(over 256 KiB)"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=JsonQueryInput,
        result_example={"result": {"id": "c2"}},
        result_schema={"type": "object", "properties": {"result": {}}, "required": ["result"]},
        example={"data": {"items": [{"id": "c1"}, {"id": "c2"}]}, "expression": "items[?id=='c2'] | [0]"},
    ),
    "html.extract": ToolDeclaration(
        id="html.extract",
        title="Extract from HTML",
        description=(
            "Read an HTML page (http.request's text, up to 2 MiB) without a browser. base_url is the https URL it came "
            "from. mode article returns {title, text, published_at, canonical_url, og_image, description, lang, "
            "graphemes}: text is the main readable content with navigation, ads, scripts and styles removed, cut at "
            "max_graphemes (default 4000); published_at is ISO or null; og_image is an absolute https URL or null "
            "(a link-card thumbnail for http.request body_from_url). mode links returns {items: [{id l1.., url, title, "
            "order}]}: the anchors (under root_selector, a CSS selector, if given) whose absolute URL matches one of "
            "include_patterns (RE2 regular expressions: at most 10 of 200 characters, no look-around or back-references; "
            "at least one) and none of exclude_patterns, de-duplicated "
            "(fragment, utm_* and fbclid dropped), the first limit (default 20, at most 60) in page order, and a "
            "dropped {scheme, host, duplicate, pattern, no_title, excluded_url, excluded_term} count. "
            "exclude_urls (links mode, at most 200, compared by the normalised URL) drops the links already handled, "
            "e.g. the post links from a getAuthorFeed, so limit counts only new links. "
            "exclude_terms (links mode, at most 50, case-insensitive) drops a link whose title contains one, "
            "e.g. 新聞稿 or 美通社 for press releases. Only https "
            "links are returned (javascript:, data: and http: are dropped), and a link to "
            "another host than base_url's is dropped unless the host is in allowed_hosts; og_image likewise needs "
            "its host to be base_url's or in allowed_hosts, else null. The output is plain text (all markup, "
            "scripts, styles and frames removed). Pure: it fetches nothing; a page of over 50,000 elements or 150 "
            "nesting levels is refused. Errors are results: {error: {code}} with html_too_large, html_unparseable, "
            "html_too_complex, no_patterns, invalid_pattern, selector_invalid, base_url_invalid"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=HtmlExtractInput,
        result_example={"title": "…", "text": "the article text", "published_at": "2026-11-18T08:00:00+00:00", "canonical_url": "https://news.example.com/a/1", "og_image": "https://news.example.com/a/1.jpg", "description": "…", "lang": "zh-Hant", "graphemes": 16},
        result_schema={"type": "object", "properties": {
            "title": {"type": "string"}, "text": {"type": "string"}, "published_at": {"type": ["string", "null"]},
            "canonical_url": {"type": ["string", "null"]}, "og_image": {"type": ["string", "null"]},
            "description": {"type": "string"}, "lang": {"type": ["string", "null"]}, "graphemes": {"type": "integer"},
            "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "url": {"type": "string"}, "title": {"type": "string"}, "order": {"type": "integer"}}}},
            "dropped": {"type": "object", "additionalProperties": {"type": "integer"}},
            "error": {"type": "object", "properties": {"code": {"type": "string"}}},
        }},
        example={
            "html": "<the text of an http.request GET>", "base_url": "https://news.example.com/section/politics",
            "mode": "links", "root_selector": "main", "include_patterns": ["^https://news\\.example\\.com/story/\\d+"],
            "exclude_patterns": ["/video/"], "limit": 10,
        },
    ),
    "text.check": ToolDeclaration(
        id="text.check",
        title="Check a text",
        description=(
            "Clean a text (trim, drop think-tags, a surrounding quote pair and a leading 摘要：) and run mechanical "
            "checks on it: grapheme length, no URLs, hashtags, emoji or Simplified Chinese, forbidden terms, a "
            "rumour term needs an attribution term, a minimum share of Han characters, and no long run copied from "
            "a source (verbatim fails when the longest shared run is >= max_verbatim_run, so 15 refuses a text that "
            "shares 15 characters in a row with the source and allows 14). Returns {ok, text, reason, graphemes}; reason is the first failed check (empty, too_long, "
            "too_short, url, hashtag, emoji, simplified, forbidden_term, unattributed_rumour, not_chinese, "
            "verbatim) or null. Pure"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=TextCheckInput,
        result_example={"ok": True, "text": "the cleaned text", "reason": None, "graphemes": 16},
        result_schema={"type": "object", "properties": {"ok": {"type": "boolean"}, "text": {"type": "string"}, "reason": {"type": ["string", "null"]}, "graphemes": {"type": "integer"}}},
        example={
            "text": "the draft to check", "source": "the material it must not copy",
            "rules": {
                "min_graphemes": 20, "max_graphemes": 300, "forbid_urls": True, "forbid_hashtags": True,
                "forbid_emoji": True, "forbid_simplified_chinese": True, "forbid_terms": ["a banned word"],
                "rumour_terms": ["allegedly"], "attribution_terms": ["according to reports"],
                "min_han_ratio": 0.5, "max_verbatim_run": 15,
            },
        },
    ),
}


# Platform tools (RAG and memory milestone). They are added to a run by the platform, never listed
# in `AgentSpec.tools`: `retrieve` when the spec attaches knowledge bases, `update_working_memory`
# when working memory is set and not read-only. No built-in or MCP tool may take these model-facing
# names. `final_result` (agent capabilities milestone) is the output tool of a spec with
# structured output (eap_schemas.output); it is reserved the same way. `knowledge.search` is the
# hidden tool behind the knowledge base search tester (tool runs only; excluded from the agent tool
# picker).
RETRIEVE_TOOL_NAME = "retrieve"
UPDATE_WORKING_MEMORY_TOOL_NAME = "update_working_memory"
OUTPUT_TOOL_NAME = "final_result"
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {RETRIEVE_TOOL_NAME, UPDATE_WORKING_MEMORY_TOOL_NAME, OUTPUT_TOOL_NAME}
)
KNOWLEDGE_SEARCH_TOOL_ID = "knowledge.search"
MAX_SEARCH_QUERY_CHARS = 2000


class KnowledgeSearchInput(BaseModel):
    """Input of the hidden ``knowledge.search`` tool (the knowledge base search tester, RM.11).

    The tenant always comes from the run, never from this input. `filter` follows the spec filter
    rules (`eap_schemas.knowledge.validate_filter`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_base_id: str
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS)
    top_k: int = Field(default=8, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    filter: dict[str, Any] | None = None

    @field_validator("knowledge_base_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        import uuid

        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ValueError("knowledge_base_id must be a UUID") from None

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("filter")
    @classmethod
    def _filter(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        from eap_schemas.knowledge import validate_filter

        checked = validate_filter(value)
        return None if checked is None else {k: list(v) if isinstance(v, tuple) else v for k, v in checked.items()}


# Hidden platform tools: registered in the worker for tool runs only, never in TOOL_DECLARATIONS
# (so never in the tool catalog, the agent tool picker or a workflow tool step), and refused in an
# agent spec's `tools` at publish and at run time.
KNOWLEDGE_SEARCH_DECLARATION = ToolDeclaration(
    id=KNOWLEDGE_SEARCH_TOOL_ID,
    title="Knowledge base search",
    description="Search one knowledge base of the tenant (the knowledge base search tester)",
    side_effect=False,
    external_idempotent=False,
    input_model=KnowledgeSearchInput,
)
HIDDEN_TOOL_DECLARATIONS: dict[str, ToolDeclaration] = {KNOWLEDGE_SEARCH_TOOL_ID: KNOWLEDGE_SEARCH_DECLARATION}


# `secrets.request` (secret input milestone): asks the person for a credential through the approval card.
SECRETS_REQUEST_TOOL_ID = "secrets.request"

# The Architect's tools that change what is live or what runs by itself. A built-in agent that lists
# one must list it in `approvals.tools` too (defined here so the shared set below needs no import of
# `eap_schemas.architect`, which imports this package).
ARCHITECT_GATED_TOOL_IDS: tuple[str, ...] = (
    "architect.publish_agent",
    "architect.restore_agent_version",
    "architect.archive_agent",
    "architect.publish_workflow",
    "architect.restore_workflow_version",
    "architect.archive_workflow",
    "architect.set_workflow_schedule",
)

# Tools that never run without a person's recorded approval: the API refuses a spec that lists one
# without an `approvals.tools` entry (422 approval_required) and the worker refuses the run.
ALWAYS_GATED_TOOL_IDS: frozenset[str] = frozenset({SECRETS_REQUEST_TOOL_ID}) | frozenset(ARCHITECT_GATED_TOOL_IDS)


def ungated_always_gated_tools(tools: Iterable[str], approvals_tools: Iterable[str] | None) -> list[str]:
    """The always-gated tools in `tools` that `approvals_tools` (spec.approvals.tools) does not list."""
    gated = set(approvals_tools or ())
    return [t for t in tools if t in ALWAYS_GATED_TOOL_IDS and t not in gated]


def builtin_tool_name(tool_id: str) -> str:
    """The model-facing name of a built-in tool id (dots become `__`)."""
    return tool_id.replace(".", "__")
