"""reStructuredText extractor (RAG P1, worker side; runs in the sandbox child).

``extract_rst(text, options, *, title, source_url) -> Document``: docutils parses the text into a
doctree with hardened settings (no file insertion, no raw, no config files, no network, nothing
reported or halted), and a small writer turns the doctree into the same ``Section`` model as
``extract()``: sections become headings whose anchors are docutils' ids (``source_url#<id>``), and
bodies become Markdown-like text (fenced code, "Note:" admonitions, pipe-row tables).

Sphinx directives and roles are resolved from this module's own table, swapped into docutils'
lookups only while a parse runs (under a lock, restored afterwards), so nothing is registered
globally:

- ``code-block``/``sourcecode`` -> literal blocks (fenced in the text);
- ``note``, ``warning``, ``tip``, ``important``, ``attention`` (docutils' own) -> "Note: ..." and
  so on; Sphinx's ``seealso``, ``versionadded``, ``versionchanged`` and ``deprecated`` likewise;
- ``toctree``, ``tabularcolumns``, ``index``, ``highlight`` (and a few other layout-only ones) ->
  dropped; ``include``, ``literalinclude`` and ``raw`` -> dropped with a warning; ``csv-table`` keeps
  its inline content and drops ``:file:``/``:url:`` with a warning (nothing is ever read or fetched);
- ``ifconfig`` and ``only`` -> evaluated against ``rst_config`` and ``rst_tags`` by a tiny safe
  evaluator (``evaluate_condition``); true -> kept, false -> dropped, unevaluable -> kept with an
  "Applies when: <condition>" line and the condition recorded on the section;
- any other unknown directive -> its argument and content kept as text;
- unknown roles (``:ref:``, ``:doc:``, ``:file:``, ...) -> their text (a ``:ref:`` to a label in the
  same document renders the section title).

NUL and the other C0 control characters (except tab and newline) are stripped from the input and
from every output string: docutils' backslash escapes leave NULs that Postgres ``text`` rejects.
"""

from __future__ import annotations

import ast
import io
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from docutils import nodes, utils
from docutils.core import publish_doctree
from docutils.parsers.rst import Directive, directives, roles
from docutils.parsers.rst.directives.tables import CSVTable
from eap_schemas.knowledge import ExtractConfig

from eap_knowledge.chunking import MAX_CONDITION_CHARS, _clip
from eap_knowledge.errors import ExtractionFailed
from eap_knowledge.extract import MAX_SECTIONS, _build, _normalize
from eap_knowledge.formats._common import check_output_size, fence, output_budget
from eap_knowledge.model import Document, Section

EXTRACTOR = "rst"
# docutils' own guard: a longer line makes it give up on the whole document, so it fails clearly.
LINE_LENGTH_LIMIT = 10_000
# Conditions longer than this are not parsed (kept, unevaluated).
MAX_CONDITION_SOURCE = 1_000
_MAX_EVAL_DEPTH = 32

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")

# Hardened docutils settings (RAG P1 decision "reStructuredText extractor"). `traceback` makes a
# docutils failure raise instead of printing and exiting.
_SETTINGS: dict[str, Any] = {
    "file_insertion_enabled": False,
    "raw_enabled": False,
    "_disable_config": True,
    "report_level": 5,
    "halt_level": 5,
    "exit_status_level": 5,
    "traceback": True,
    "line_length_limit": LINE_LENGTH_LIMIT,
    "input_encoding": "unicode",
    "doctitle_xform": False,
    "sectsubtitle_xform": False,
    "docinfo_xform": False,
    "smart_quotes": False,
    "syntax_highlight": "none",
    "language_code": "en",
    "pep_references": False,
    "rfc_references": False,
    "character_level_inline_markup": False,
    "strip_comments": True,
    "id_prefix": "",
}


def strip_controls(text: str) -> str:
    """``text`` without NUL and the other C0 control characters (tab and newline kept) and DEL."""
    return _CONTROL.sub("", text)


# --- the condition evaluator ---------------------------------------------------------------------


class _Unevaluable(Exception):
    pass


def evaluate_condition(expr: str, lookup: Callable[[str], object]) -> bool | None:
    """Evaluate a Sphinx ``ifconfig``/``only`` condition without ``eval``; None when it cannot be.

    Allowed: names (resolved by ``lookup``, which raises ``KeyError`` for an unknown one), string,
    number and boolean literals, ``==``, ``!=``, ``in``/``not in`` over a tuple or list literal (or
    a string literal, with Python's substring meaning, as Sphinx evaluates ``x in ('AM62X')``),
    ``and``, ``or``, ``not`` and parentheses. Anything else (calls, attributes, subscripts,
    arithmetic, comprehensions, lambdas ...) makes the whole condition unevaluable. ``ast.parse``
    only parses; nothing is executed.
    """
    if len(expr) > MAX_CONDITION_SOURCE:
        return None
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    try:
        return bool(_eval(tree.body, lookup, 0))
    except _Unevaluable:
        return None


def _eval(node: ast.AST, lookup: Callable[[str], object], depth: int) -> object:
    if depth > _MAX_EVAL_DEPTH:
        raise _Unevaluable
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return node.value
        raise _Unevaluable
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        try:
            return lookup(node.id)
        except KeyError:
            raise _Unevaluable from None
    if isinstance(node, ast.BoolOp):
        values = [bool(_eval(v, lookup, depth + 1)) for v in node.values]  # strict: all must evaluate
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, lookup, depth + 1)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, lookup, depth + 1)
        result = True
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.In, ast.NotIn)):
                right = _container(comparator, lookup, depth + 1)
                if isinstance(right, str) and not isinstance(left, str):
                    raise _Unevaluable
                hit = left in right
                ok = hit if isinstance(op, ast.In) else not hit
            elif isinstance(op, (ast.Eq, ast.NotEq)):
                right = _eval(comparator, lookup, depth + 1)
                ok = (left == right) if isinstance(op, ast.Eq) else (left != right)
            else:
                raise _Unevaluable
            result = result and ok
            left = right
        return result
    raise _Unevaluable


def _container(node: ast.AST, lookup: Callable[[str], object], depth: int) -> tuple[object, ...] | str:
    if isinstance(node, (ast.Tuple, ast.List)) and isinstance(node.ctx, ast.Load):
        return tuple(_eval(e, lookup, depth + 1) for e in node.elts)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise _Unevaluable


def _config_lookup(config: Mapping[str, object] | None) -> Callable[[str], object]:
    def lookup(name: str) -> object:
        if config is None:
            raise KeyError(name)
        return config[name]

    return lookup


def _tags_lookup(tags: Sequence[str] | None) -> Callable[[str], object]:
    tag_set = None if tags is None else frozenset(tags)

    def lookup(name: str) -> object:
        if tag_set is None:
            raise KeyError(name)
        return name in tag_set

    return lookup


# --- per-extraction state and the Sphinx directive/role table -------------------------------------


@dataclass
class _Run:
    config: Mapping[str, object] | None
    tags: Sequence[str] | None
    warnings: list[str] = field(default_factory=list)


_RUN: ContextVar[_Run] = ContextVar("eap_rst_run")
# docutils' directive and role lookups are module globals; a parse swaps them, so parses are serial.
_LOCK = threading.Lock()


class _AnyOptions(dict):  # type: ignore[type-arg]
    """An option spec that accepts every option as an unchanged string (Sphinx options)."""

    def __missing__(self, key: str) -> Callable[[str | None], str]:
        return directives.unchanged

    def __bool__(self) -> bool:  # docutils treats an empty (falsy) spec as "takes no options"
        return True


class _CodeBlock(Directive):
    optional_arguments = 1
    final_argument_whitespace = True
    option_spec = _AnyOptions()
    has_content = True

    def run(self) -> list[nodes.Node]:
        text = "\n".join(self.content)
        node = nodes.literal_block(text, text)
        node["language"] = self.arguments[0].strip() if self.arguments else ""
        out: list[nodes.Node] = []
        caption = self.options.get("caption")
        if caption:
            out.append(nodes.paragraph("", caption))
        return [*out, node]


class _Drop(Directive):
    optional_arguments = 1
    final_argument_whitespace = True
    option_spec = _AnyOptions()
    has_content = True

    def run(self) -> list[nodes.Node]:
        return []


def _dropping_with_warning(code: str) -> type[Directive]:
    class DropWithWarning(_Drop):
        def run(self) -> list[nodes.Node]:
            _RUN.get().warnings.append(code)
            return []

    return DropWithWarning


class _SafeCSVTable(CSVTable):
    option_spec = _AnyOptions(CSVTable.option_spec)

    def run(self) -> list[nodes.Node]:
        if "file" in self.options or "url" in self.options:
            self.options.pop("file", None)
            self.options.pop("url", None)
            _RUN.get().warnings.append("csv_table_source_dropped")
        if not self.content:
            return []
        return super().run()


def _labelled(label: str, with_argument: bool) -> type[Directive]:
    """Sphinx's ``seealso``/``versionadded``-style directives as an admonition titled ``label``."""

    class Labelled(Directive):
        optional_arguments = 1
        final_argument_whitespace = True
        option_spec = _AnyOptions()
        has_content = True

        def run(self) -> list[nodes.Node]:
            arg = " ".join(self.arguments).strip()
            title = f"{label} {arg}" if with_argument and arg else label
            box = nodes.admonition()
            box += nodes.title(title, title)
            if self.content:
                self.state.nested_parse(self.content, self.content_offset, box)
            elif arg and not with_argument:
                box += nodes.paragraph(arg, arg)
            return [box]

    return Labelled


class _Conditional(Directive):
    """``ifconfig`` (against ``rst_config``) and ``only`` (against ``rst_tags``)."""

    required_arguments = 1
    final_argument_whitespace = True
    option_spec = _AnyOptions()
    has_content = True

    def run(self) -> list[nodes.Node]:
        run = _RUN.get()
        expr = _WS.sub(" ", strip_controls(self.arguments[0])).strip()
        lookup = _tags_lookup(run.tags) if self.name.lower() == "only" else _config_lookup(run.config)
        verdict = evaluate_condition(expr, lookup)
        if verdict is False:
            return []
        box = nodes.container()
        if verdict is None:
            box["eap_condition"] = _clip(expr, MAX_CONDITION_CHARS)
        self.state.nested_parse(self.content, self.content_offset, box, match_titles=True)
        return [box]


def _generic(name: str) -> type[Directive]:
    """An unknown directive: its argument as a paragraph, then its content parsed as rst."""

    class Generic(Directive):
        optional_arguments = 1
        final_argument_whitespace = True
        option_spec = _AnyOptions()
        has_content = True

        def run(self) -> list[nodes.Node]:
            arg = " ".join(self.arguments).strip()
            box = nodes.container()
            if arg:
                box += nodes.paragraph(arg, arg)
            if self.content:
                self.state.nested_parse(self.content, self.content_offset, box, match_titles=True)
            return [box]

    Generic.__name__ = "Generic"
    return Generic


_DROPPED = (
    "toctree", "tabularcolumns", "index", "highlight", "contents", "sectnum", "meta", "rst-class",
    "cssclass", "sectionauthor", "moduleauthor", "codeauthor", "default-domain", "currentmodule",
    "header", "footer",
)
_DIRECTIVES: dict[str, type[Directive]] = {
    **{name: _Drop for name in _DROPPED},
    "code-block": _CodeBlock,
    # docutils' own `code` would put `:number-lines:` numbers into the code text; ours ignores options.
    "code": _CodeBlock,
    "sourcecode": _CodeBlock,
    "include": _dropping_with_warning("include_dropped"),
    "literalinclude": _dropping_with_warning("literalinclude_dropped"),
    "raw": _dropping_with_warning("raw_dropped"),
    "csv-table": _SafeCSVTable,
    "ifconfig": _Conditional,
    "only": _Conditional,
    "seealso": _labelled("See also", with_argument=False),
    "versionadded": _labelled("New in version", with_argument=True),
    "versionchanged": _labelled("Changed in version", with_argument=True),
    "deprecated": _labelled("Deprecated since version", with_argument=True),
}

# Sphinx roles shown as inline code; every other unknown role shows its text.
_LITERAL_ROLES = frozenset(
    {
        "file", "command", "program", "samp", "console", "kbd", "envvar", "option", "makevar",
        "guilabel", "menuselection", "mailheader", "regexp", "c", "cpp", "py", "func", "meth",
        "class", "data", "const", "attr", "obj", "mod", "macro", "type", "struct", "member",
    }
)
_ROLE_TARGET = re.compile(r"^(.*?)\s*<([^<>]+)>$", re.S)


def _generic_role(
    name: str,
    rawtext: str,
    text: str,
    lineno: int,
    inliner: object,
    options: object = None,
    content: object = None,
) -> tuple[list[nodes.Node], list[nodes.system_message]]:
    plain = utils.unescape(text)
    match = _ROLE_TARGET.match(plain)
    explicit = bool(match and match.group(1).strip())
    display = (match.group(1) if explicit else match.group(2)) if match else plain
    display = display.strip().lstrip("~!")
    short = name.lower().rsplit(":", 1)[-1]
    if short in _LITERAL_ROLES:
        return [nodes.literal(display, display)], []
    node = nodes.inline(display, display)
    if short == "ref" and not explicit:
        node["eap_ref"] = display
    return [node], []


def _raw_role(*args: object, **kwargs: object) -> tuple[list[nodes.Node], list[nodes.system_message]]:
    _RUN.get().warnings.append("raw_dropped")
    return [], []


# `.. role:: raw-html(raw)` parses its options against the base role's spec; accept any.
_raw_role.options = _AnyOptions()  # type: ignore[attr-defined]
_raw_role.content = False  # type: ignore[attr-defined]


@contextmanager
def _sphinx_lookups() -> Iterator[None]:
    original_directive = directives.directive
    original_role = roles.role

    def directive(name: str, language_module: object, document: object) -> tuple[Any, list[Any]]:
        known = _DIRECTIVES.get(name.lower())
        if known is not None:
            return known, []
        found, messages = original_directive(name, language_module, document)
        return (found, messages) if found is not None else (_generic(name), [])

    def role(name: str, language_module: object, lineno: int, reporter: object) -> tuple[Any, list[Any]]:
        if name.lower() == "raw":  # also the base of `.. role:: raw-html(raw)`: never any markup
            return _raw_role, []
        found, messages = original_role(name, language_module, lineno, reporter)
        if found is not None:
            return found, messages
        return (lambda *args, **kwargs: _generic_role(name, *args[1:], **kwargs)), []

    # `.. role::` registers into docutils' module-level role table; a parse must not leak its roles
    # (or any cached lookup) into the next one, so both tables are restored afterwards.
    saved_roles = dict(roles._roles)
    saved_directives = dict(directives._directives)
    directives.directive = directive
    roles.role = role
    try:
        yield
    finally:
        directives.directive = original_directive
        roles.role = original_role
        roles._roles.clear()
        roles._roles.update(saved_roles)
        directives._directives.clear()
        directives._directives.update(saved_directives)


# --- the writer: doctree -> raw sections ----------------------------------------------------------


_ADMONITION_LABELS = {
    "note": "Note", "warning": "Warning", "tip": "Tip", "important": "Important",
    "attention": "Attention", "caution": "Caution", "danger": "Danger", "error": "Error",
    "hint": "Hint",
}
_REFUSED_TITLE = re.compile(r"([^\w\s])\1+")


@dataclass
class _RawSection:
    level: int
    heading: str
    anchor: str | None
    blocks: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    # A continuation of an earlier section (its index in _Writer.sections): the text that follows a
    # directive whose content opened sections belongs to the section the directive is in.
    continuation_of: int | None = None


class _Writer:
    def __init__(self, document: nodes.document) -> None:
        self.document = document
        self.sections: list[_RawSection] = [_RawSection(0, "", None)]

    # Structure ---------------------------------------------------------------------------------

    def walk(self, node: nodes.Element, level: int, conditions: tuple[str, ...]) -> None:
        for child in node.children:
            if isinstance(child, nodes.section):
                title = child.next_node(nodes.title)
                ids = child.get("ids") or []
                heading = self.inline(title) if title is not None else ""
                self.sections.append(
                    _RawSection(level + 1, _collapse(heading), ids[0] if ids else None, [], list(conditions))
                )
                rest = nodes.Element()
                rest.children = [c for c in child.children if c is not title]
                self.walk(rest, level + 1, conditions)
            elif isinstance(child, nodes.container) and not child.get("classes"):
                condition = child.get("eap_condition")
                inner = conditions
                if condition:
                    self.current.blocks.append(f"Applies when: {condition}")
                    self._note(condition)
                    inner = (*conditions, condition)
                before = len(self.sections)
                owner = self.current
                origin = owner.continuation_of if owner.continuation_of is not None else before - 1
                self.walk(child, level, inner)
                if len(self.sections) > before:  # the content opened sections: continue the owner
                    self.sections.append(
                        _RawSection(owner.level, owner.heading, owner.anchor, [], list(conditions), origin)
                    )
            else:
                self.current.blocks.extend(self.block(child))

    @property
    def current(self) -> _RawSection:
        return self.sections[-1]

    def _note(self, condition: str) -> None:
        if condition not in self.current.conditions:
            self.current.conditions.append(condition)

    # Inline ------------------------------------------------------------------------------------

    def inline(self, node: nodes.Node) -> str:
        parts: list[str] = []
        for child in node.children:
            if isinstance(child, nodes.Text):
                parts.append(child.astext())
            elif isinstance(child, nodes.emphasis):
                text = self.inline(child).strip()
                parts.append(f"*{text}*" if text else "")
            elif isinstance(child, nodes.strong):
                text = self.inline(child).strip()
                parts.append(f"**{text}**" if text else "")
            elif isinstance(child, nodes.literal):
                text = child.astext().replace("\n", " ")
                tick = "``" if "`" in text else "`"
                parts.append(f"{tick}{text}{tick}" if text else "")
            elif isinstance(child, nodes.reference):
                text = self.inline(child).strip()
                uri = child.get("refuri") or ""
                if uri.startswith(("http://", "https://")) and text and text != uri:
                    parts.append(f"[{text}]({uri})")
                else:
                    parts.append(text or uri)
            elif isinstance(child, (nodes.footnote_reference, nodes.citation_reference)):
                parts.append(f"[{child.astext()}]")
            elif isinstance(child, (nodes.image, nodes.system_message, nodes.comment, nodes.raw)):
                continue
            elif isinstance(child, nodes.math):
                parts.append(f"${child.astext()}$")
            elif isinstance(child, nodes.inline) and child.get("eap_ref"):
                parts.append(self._ref_title(child["eap_ref"]) or child.astext())
            elif isinstance(child, nodes.Element):
                parts.append(self.inline(child))
        return "".join(parts)

    def _ref_title(self, label: str) -> str:
        """The title of the section a same-document ``:ref:`` label points at, or ""."""
        node_id = self.document.nameids.get(nodes.fully_normalize_name(label))
        target = self.document.ids.get(node_id) if node_id else None
        if isinstance(target, nodes.section):
            title = target.next_node(nodes.title)
            return _collapse(self.inline(title)) if title is not None else ""
        return ""

    def para(self, node: nodes.Node) -> str:
        return _collapse(self.inline(node))

    # Blocks ------------------------------------------------------------------------------------

    def blocks(self, node: nodes.Node) -> list[str]:
        out: list[str] = []
        for child in node.children:
            out.extend(self.block(child))
        return [b for b in out if b.strip()]

    def block(self, node: nodes.Node) -> list[str]:  # noqa: C901 - one dispatch table
        n = nodes
        if isinstance(node, n.paragraph):
            return [self.para(node)]
        if isinstance(node, (n.literal_block, n.doctest_block)):
            return [_fence(node.astext(), _language(node))]
        if isinstance(node, n.math_block):
            return [_fence(node.astext(), "math")]
        if isinstance(node, (n.bullet_list, n.enumerated_list)):
            return [self.list_block(node)]
        if isinstance(node, n.definition_list):
            out: list[str] = []
            for item in node.children:
                term = item.next_node(n.term)
                if term is not None:
                    out.append(f"**{self.para(term)}**")
                definition = item.next_node(n.definition)
                if definition is not None:
                    out.extend(self.blocks(definition))
            return out
        if isinstance(node, n.field_list):
            out = []
            for item in node.children:
                name = item.next_node(n.field_name)
                body = item.next_node(n.field_body)
                text = " ".join(self.blocks(body)) if body is not None else ""
                out.append(f"**{self.para(name) if name is not None else ''}:** {text}".rstrip())
            return out
        if isinstance(node, n.option_list):
            return [_fence(node.astext())]
        if isinstance(node, n.Admonition):
            label = _ADMONITION_LABELS.get(node.tagname, "")
            inner: list[str] = []
            for child in node.children:
                if isinstance(child, n.title):
                    label = self.para(child)
                else:
                    inner.extend(self.block(child))
            if label and inner:
                inner[0] = f"{label}: {inner[0]}"
            elif label:
                inner = [f"{label}:"]
            return inner
        if isinstance(node, n.table):
            return self.table(node)
        if isinstance(node, n.line_block):
            return ["\n".join(self.para(line) for line in node.findall(n.line))]
        if isinstance(node, n.figure):
            out = []
            for child in node.children:
                if isinstance(child, n.caption):
                    out.append(f"Figure: {self.para(child)}")
                elif isinstance(child, n.legend):
                    out.extend(self.blocks(child))
            return out
        if isinstance(node, (n.footnote, n.citation)):
            label = node.next_node(n.label)
            body = " ".join(b for child in node.children if not isinstance(child, n.label) for b in self.block(child))
            return [f"[{label.astext() if label is not None else ''}] {body}".strip()]
        if isinstance(node, (n.topic, n.sidebar)):
            out = []
            for child in node.children:
                if isinstance(child, n.title):
                    out.append(f"**{self.para(child)}**")
                else:
                    out.extend(self.block(child))
            return out
        if isinstance(node, (n.rubric, n.title, n.subtitle)):
            text = self.para(node)
            return [f"**{text}**"] if text else []
        if isinstance(node, n.section):  # a section inside a block (rare): flattened
            title = node.next_node(n.title)
            out = [f"**{self.para(title)}**"] if title is not None else []
            return out + [b for c in node.children if c is not title for b in self.block(c)]
        if isinstance(node, n.container) and node.get("eap_condition"):
            self._note(node["eap_condition"])
            return [f"Applies when: {node['eap_condition']}", *self.blocks(node)]
        if isinstance(node, n.system_message):
            return self.refused_title(node)
        if isinstance(node, (n.image, n.comment, n.target, n.substitution_definition, n.pending, n.raw,
                             n.decoration, n.transition, n.meta)):
            return []
        if isinstance(node, n.Text):
            text = node.astext().strip()
            return [text] if text else []
        if isinstance(node, n.TextElement):
            text = self.para(node)
            return [text] if text else []
        if isinstance(node, n.Element):  # block_quote, compound, container, list items, unknown
            return self.blocks(node)
        return []

    def refused_title(self, node: nodes.system_message) -> list[str]:
        """A title docutils refused (a section inside a directive) keeps its text as a line."""
        literal = node.next_node(nodes.literal_block)
        message = node.children[0].astext() if node.children else ""
        if literal is None or ("section" not in message and "title" not in message.lower()):
            return []
        for line in literal.astext().split("\n"):
            stripped = line.strip()
            if stripped and not _REFUSED_TITLE.fullmatch(stripped):
                return [f"**{stripped}**"]
        return []

    def list_block(self, node: nodes.Element) -> str:
        ordered = isinstance(node, nodes.enumerated_list)
        try:
            start = int(node.get("start", 1) or 1)
        except (TypeError, ValueError):
            start = 1
        items = []
        for i, item in enumerate(node.children):
            marker = f"{start + i}." if ordered else "-"
            text = "\n\n".join(self.blocks(item))
            lines = text.split("\n") if text else [""]
            pad = " " * (len(marker) + 1)
            items.append(f"{marker} {lines[0]}" + "".join("\n" + (pad + ln if ln else "") for ln in lines[1:]))
        return "\n".join(items)

    def table(self, node: nodes.Element) -> list[str]:
        out: list[str] = []
        title = next((c for c in node.children if isinstance(c, nodes.title)), None)
        if title is not None:
            out.append(f"**{self.para(title)}**")
        rows: list[list[str]] = []
        head_rows = 0
        for tgroup in node.findall(nodes.tgroup):
            for part in tgroup.children:
                if not isinstance(part, (nodes.thead, nodes.tbody)):
                    continue
                for row in part.children:
                    cells: list[str] = []
                    for entry in row.children:
                        text = " ".join(b.replace("\n", " ") for b in self.blocks(entry))
                        cells.append(_collapse(text).replace("|", "\\|"))
                        cells.extend([""] * int(entry.get("morecols", 0) or 0))
                    rows.append(cells)
                    if isinstance(part, nodes.thead):
                        head_rows += 1
            break  # one tgroup per table in rst
        if not rows:
            return out
        width = max(len(r) for r in rows)
        lines = []
        for i, row in enumerate(rows):
            row = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(row) + " |")
            if head_rows and i == head_rows - 1:
                lines.append("|" + "---|" * width)
        out.append("\n".join(lines))
        return out


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _language(node: nodes.Element) -> str:
    language = node.get("language") or ""
    if not language:
        classes = node.get("classes") or []
        if len(classes) >= 2 and classes[0] == "code":
            language = classes[1]
    return language.split()[0] if language.strip() else ""


_fence = fence


# --- entry point ----------------------------------------------------------------------------------


def extract_rst(
    text: str,
    options: ExtractConfig | None = None,
    *,
    title: str = "",
    source_url: str | None = None,
) -> Document:
    """Extract an rst document. ``options`` is the knowledge base's ``chunking.extract``.

    Raises ``ExtractionFailed("extract_limit")`` for a line over LINE_LENGTH_LIMIT characters or
    nesting too deep to parse, and ``ExtractionFailed("extract_failed")`` if docutils fails.
    """
    content = strip_controls(_normalize(text))
    if any(len(line) > LINE_LENGTH_LIMIT for line in content.split("\n")):
        raise ExtractionFailed("extract_limit", f"a line is over {LINE_LENGTH_LIMIT} characters")
    budget = output_budget(len(content))
    if _substitution_estimate(content, budget) > budget:
        raise ExtractionFailed("extract_limit", "substitutions expand the document too far")
    run = _Run(
        config=options.rst_config if options is not None else None,
        tags=options.rst_tags if options is not None else None,
    )
    settings = {**_SETTINGS, "warning_stream": io.StringIO()}
    with _LOCK:
        token = _RUN.set(run)
        try:
            with _sphinx_lookups():
                doctree = publish_doctree(content, settings_overrides=settings)
            if len(doctree.astext()) > budget:
                raise ExtractionFailed("extract_limit", f"the parsed document is over {budget} characters")
            writer = _Writer(doctree)
            writer.walk(doctree, 0, ())
        except RecursionError:
            raise ExtractionFailed("extract_limit", "nesting too deep") from None
        except ExtractionFailed:
            raise
        except Exception as exc:  # a docutils failure is this document's failure
            raise ExtractionFailed("extract_failed", f"docutils: {type(exc).__name__}") from None
        finally:
            _RUN.reset(token)
    doc_title = strip_controls(_collapse(_normalize(title))) or strip_controls(_collapse(str(doctree.get("title", ""))))
    document = _document(writer, doc_title, source_url, tuple(run.warnings))
    check_output_size(document.sections, len(content))
    return document


# A substitution definition's first line: `.. |name| replace:: text`, `.. |name| replace :: text`,
# or `.. |name|` alone with the directive on the next, indented line. Its body is that line's rest
# plus every following line indented deeper (an upper estimate of the replacement text).
_SUBSTITUTION = re.compile(r"^([ \t]*)\.\.[ \t]+\|([^|\n]+)\|(.*)$")
# Every pipe-to-next-pipe span, overlapping and across line breaks (docutils joins a reference split
# over two lines), so a stray `|` can never shift the pairing and hide a real reference.
_SUB_REF = re.compile(r"(?=\|([^|]{1,200}?)\|)", re.S)


def _sub_name(name: str) -> str:
    return " ".join(name.split()).lower()


def _substitution_estimate(content: str, budget: int) -> int:
    """How long ``content`` gets once every substitution reference is replaced (an upper estimate,
    capped just past ``budget``), so a few definitions used thousands of times, or nested inside one
    another, fail before docutils builds the expanded tree."""
    lines = content.split("\n")
    definitions: dict[str, str] = {}
    i = 0
    while i < len(lines):  # one pass: a definition's continuation lines are consumed with it
        match = _SUBSTITUTION.match(lines[i])
        i += 1
        if not match:
            continue
        indent, name, body = len(match.group(1)), _sub_name(match.group(2)), [match.group(3).strip()]
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            body.append(lines[i].strip())
            i += 1
        definitions.setdefault(name, " ".join(b for b in body if b))
    if not definitions:
        return len(content)
    sizes: dict[str, int] = {}

    def size(name: str, active: frozenset[str]) -> int:
        if name in sizes:
            return sizes[name]
        text = definitions[name]
        total = len(text)
        for ref in _SUB_REF.findall(text):
            ref = _sub_name(ref)
            if ref in definitions and ref not in active:
                total = min(total + size(ref, active | {name}), budget + 1)
        sizes[name] = total
        return total

    total = len(content)
    for ref in _SUB_REF.findall(content):
        ref = _sub_name(ref)
        if ref in definitions:
            total += size(ref, frozenset())
            if total > budget:
                break
    return total


def _document(writer: _Writer, title: str, source_url: str | None, warnings: tuple[str, ...]) -> Document:
    # Continuations without text go; the rest keep their index into `kept` for the fix-up below.
    kept = [
        i for i, s in enumerate(writer.sections)
        if s.continuation_of is None or any(b.strip() for b in s.blocks)
    ]
    position = {old: new for new, old in enumerate(kept)}
    chosen = [writer.sections[i] for i in kept]
    raw = [
        (s.level, strip_controls(s.heading), strip_controls("\n\n".join(b for b in s.blocks if b.strip())), s.anchor)
        for s in chosen
    ]
    conditions = [list(s.conditions) for s in chosen]
    if len(conditions) > MAX_SECTIONS:  # _build folds the tail into the last section; so do these
        tail = [c for group in conditions[MAX_SECTIONS - 1 :] for c in group]
        conditions = [*conditions[: MAX_SECTIONS - 1], list(dict.fromkeys(tail))]
    built = list(_build(raw))
    for i, s in enumerate(chosen[: len(built)]):
        owner = position.get(s.continuation_of) if s.continuation_of is not None else None
        if owner is not None and owner < i:  # the owner's heading path and anchor, exactly
            built[i] = replace(built[i], heading_path=built[owner].heading_path, anchor=built[owner].anchor)
    sections = tuple(
        replace(section, condition=_condition(conds)) if conds else section
        for section, conds in zip(built, conditions, strict=True)
    )
    return Document(
        title=title,
        source_url=source_url or None,
        format=EXTRACTOR,
        sections=sections,
        extracted_chars=_non_ws_len(sections),
        warnings=warnings,
    )


def _condition(conditions: list[str]) -> str | None:
    if not conditions:
        return None
    return _clip("; ".join(dict.fromkeys(conditions)), MAX_CONDITION_CHARS)


def _non_ws_len(sections: tuple[Section, ...]) -> int:
    return sum(len("".join((s.heading + s.text).split())) for s in sections)


__all__ = ["EXTRACTOR", "LINE_LENGTH_LIMIT", "evaluate_condition", "extract_rst", "strip_controls"]
