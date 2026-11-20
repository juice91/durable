"""`html.extract` (eap_worker.html_extract_tool): the readable article or the matching links of an HTML page. Pure."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, HtmlExtractInput
from eap_worker.html_extract_tool import html_extract

LTN = "https://news.example.com/politics/breakingnews/5000001"

ARTICLE = """<!doctype html>
<html lang="zh-Hant"><head>
<title>市府宣布新政策 - 範例新聞</title>
<meta property="og:title" content="市府宣布新政策">
<meta property="og:description" content="市府今天宣布一項新的交通政策。">
<meta property="og:image" content="/images/cover.jpg">
<meta property="article:published_time" content="2026-11-18T08:30:00+08:00">
<link rel="canonical" href="/politics/breakingnews/5000001/">
<script>window.ad = "<script>bad()</script>";</script>
<style>.x{color:red}</style>
</head><body>
<header><nav><a href="/">首頁</a><a href="/politics">政治</a></nav></header>
<div class="breadcrumb">首頁 &gt; 政治</div>
<div class="wrap has-sidebar">
  <div class="main">
    <h1>市府宣布新政策</h1>
    <article>
      <p>市府今天宣布一項新的交通政策，預計明年起在市區主要道路實施，並同步調整公車路線。</p>
      <div class="ad-slot">廣告：買一送一，限時優惠，請立即點擊連結。</div>
      <p>市長表示，這項政策是經過半年的意見蒐集後定案，希望能減少尖峰時段的壅塞情形。</p>
      <script>track("x")</script>
      <p>反對者則認為，配套措施仍不完整，呼籲市府在實施前再與居民溝通。</p>
      <div class="related"><a href="/a">相關新聞：另一則</a><a href="/b">相關新聞：又一則</a></div>
    </article>
  </div>
  <aside><p>熱門排行：這是一段很長很長很長很長很長的側欄文字，不應出現在內文。</p></aside>
</div>
<footer><p>版權所有，未經許可不得轉載，本網站所有內容受著作權法保護。</p></footer>
</body></html>"""


def article(html, **kw):
    return html_extract(None, html=html, base_url=kw.pop("base_url", LTN), mode="article", **kw)


def links(html, **kw):
    kw.setdefault("include_patterns", [r"."])
    return html_extract(None, html=html, base_url=kw.pop("base_url", "https://www.mirrormedia.example/section/politics"), mode="links", **kw)


# ---------------------------------------------------------------------------- article


def test_an_ltn_style_article():
    out = article(ARTICLE)
    assert out["title"] == "市府宣布新政策"
    assert out["lang"] == "zh-Hant"
    assert out["published_at"] == "2026-11-18T08:30:00+08:00"
    assert out["canonical_url"] == "https://news.example.com/politics/breakingnews/5000001/"
    assert out["og_image"] == "https://news.example.com/images/cover.jpg"
    assert out["description"] == "市府今天宣布一項新的交通政策。"
    assert "新的交通政策" in out["text"] and "再與居民溝通" in out["text"]
    for noise in ("廣告", "相關新聞", "熱門排行", "版權所有", "首頁", "track(", "bad()", "color:red"):
        assert noise not in out["text"], noise
    assert out["text"].count("\n") == 2  # three paragraphs, one per line
    assert out["graphemes"] == len(out["text"].replace("\n", "")) + 2


def test_an_article_without_an_article_element_still_finds_the_paragraphs():
    html = (
        "<html><body><div class='top'><a href='/'>Home</a></div><div class='story'>"
        + "".join(f"<p>Paragraph number {i} has enough words to count as readable text.</p>" for i in range(5))
        + "</div><div class='links'><p><a href='/x'>x</a> <a href='/y'>y</a></p></div></body></html>"
    )
    out = article(html)
    assert out["text"].splitlines()[0].startswith("Paragraph number 0") and len(out["text"].splitlines()) == 5


def test_published_at_from_json_ld_then_time_then_null():
    ld = '<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},{"@type":"NewsArticle","datePublished":"2026-11-17T01:02:03Z"}]}</script>'
    assert article(f"<html><head>{ld}</head><body><time datetime='2026-01-01'>x</time><p>{'a' * 30}</p></body></html>")["published_at"] == "2026-11-17T01:02:03+00:00"
    assert article("<html><body><time datetime='2026-11-16T10:00:00+08:00'>x</time></body></html>")["published_at"] == "2026-11-16T10:00:00+08:00"
    assert article("<html><body><p>no date here at all</p></body></html>")["published_at"] is None
    assert article("<html><head><meta property='article:published_time' content='yesterday'></head><body>x</body></html>")["published_at"] is None


def test_images_and_canonical_must_be_absolute_https():
    page = "<html><head><meta property='og:image' content='http://cdn.example.com/a.jpg'><link rel='canonical' href='javascript:x'></head><body>x</body></html>"
    out = article(page)
    assert out["og_image"] is None and out["canonical_url"] is None
    page = "<html><head><meta property='og:image' content='//cdn.example.com/a.jpg'><meta property='og:url' content='https://news.example.com/a'></head><body>x</body></html>"
    out = article(page)
    assert out["og_image"] is None  # cdn.example.com is neither base_url's host nor allowed
    out = article(page, allowed_hosts=["cdn.example.com"])
    assert out["og_image"] == "https://cdn.example.com/a.jpg" and out["canonical_url"] == "https://news.example.com/a"
    for bad in ("data:image/png;base64,AAAA", "javascript:alert(1)", "http://news.example.com/a.jpg"):
        assert article(f"<html><head><meta property='og:image' content='{bad}'></head><body>x</body></html>")["og_image"] is None


def test_title_falls_back_to_h1_then_title_element_and_missing_parts_are_empty():
    assert article("<html><head><title>T</title></head><body><h1> The  H1 </h1></body></html>")["title"] == "The H1"
    out = article("<html><head><title>Only Title</title></head><body>x</body></html>")
    assert out["title"] == "Only Title" and out["description"] == "" and out["lang"] is None


def test_max_graphemes_caps_the_text_by_clusters():
    family = "👨‍👩‍👧"
    html = f"<html><body><article><p>{family * 40}</p></article></body></html>"
    out = article(html, max_graphemes=10)
    assert out["text"] == family * 10 and out["graphemes"] == 10
    long = "<html><body><article><p>" + "字" * 5000 + "</p></article></body></html>"
    assert article(long)["graphemes"] == 4000


def test_think_blocks_and_escaped_markup_never_reach_the_output():
    html = (
        "<html><head><meta property='og:description' content='d &lt;think&gt;secret&lt;/think&gt; e'></head><body><article>"
        "<p>Visible text that is long enough. &lt;think&gt;hidden thoughts&lt;/think&gt; After. &lt;script&gt;alert(1)&lt;/script&gt;</p>"
        "</article></body></html>"
    )
    out = article(html)
    blob = " ".join(str(v) for v in out.values())
    assert "<think" not in blob and "hidden thoughts" not in blob and "<script" not in blob.lower() and "</" not in blob
    assert "Visible text" in out["text"] and "After." in out["text"]


_PIECES = ["<iframe", "<IFRAME src=x>", " onerror=x ", "<img onload=", "<svg/onload=", '"', "'", "<", ">", "&lt;", "&gt;", "script", "SCRIPT", "/", "!", "<scr", "ipt>", "&#60;", "&amp;lt;", "字", " ", "<think>", "x"]


@pytest.mark.parametrize("seed", range(60))
def test_the_output_never_contains_a_script_tag(seed):
    rng = random.Random(seed)
    noise = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 40)))
    html = (
        f"<html><head><title>{noise}</title></head><body><article><p>{noise} and some more filler text here.</p>"
        f"<p>&lt;script&gt;x&lt;/script&gt;<script>evil()</script></p></article>"
        f"<a href='/a'>&lt;SCRIPT src=x&gt;</a></body></html>"
    )
    out = article(html)
    flat = str(out).lower()
    assert "<script" not in flat and "<iframe" not in flat and "<svg" not in flat and "<img" not in flat
    listed = links(html, base_url="https://news.example.com/")
    assert "<script" not in str(listed).lower() and "<iframe" not in str(listed).lower()


def test_attributes_and_inert_elements_are_not_text():
    html = (
        "<html><body><article><p onclick=\"alert(1)\" onmouseover=x>Click this long enough paragraph of text.</p>"
        "<iframe src='https://evil.example/x'>frame fallback</iframe><svg><text>svg words</text></svg>"
        "<noscript>noscript words</noscript><template><p>template words</p></template>"
        "<style>p{}</style></article></body></html>"
    )
    out = article(html)
    assert out["text"] == "Click this long enough paragraph of text."


# ---------------------------------------------------------------------------- links

MIRROR = """<html><body>
<nav><a href="/section/politics">政治</a><a href="/story/aaa111">導覽中的連結</a></nav>
<main class="list">
  <a href="/story/aaa111?utm_source=fb&utm_medium=x&id=7#comments"><h3>第一則新聞標題</h3></a>
  <a href="https://www.mirrormedia.example/story/aaa111/?fbclid=zzz&id=7">重複的連結</a>
  <a href="/story/bbb222/">  第二則
    新聞標題 </a>
  <a href="https://evil.example.net/story/ccc333">釣魚網站的連結</a>
  <a href="https://cdn.mirrormedia.example/story/ddd444">允許網域的連結</a>
  <a href="/video/eee555">影片連結</a>
  <a href="/story/fff666"><img src="x.jpg" alt="圖片替代文字"></a>
  <a href="/story/ggg777" aria-label="以標籤命名"></a>
  <a href="/story/hhh888" title="以 title 命名"></a>
  <a href="/story/iii999"></a>
  <a href="http://www.mirrormedia.example/story/plain-http">plain http</a>
  <a href="data:text/html,x">data</a>
  <a href="mailto:a@b.com">mail</a>
  <a href="javascript:void(0)">js</a>
  <a>no href</a>
</main></body></html>"""

CTWANT = """<html><body>
<div class="p-category__list">
  <div class="p-category__item"><a href="/article/101"><span>CTWANT 第一篇</span></a></div>
  <div class="p-category__item"><a href="https://www.ctwant.example/article/102?utm_campaign=a">CTWANT 第二篇</a></div>
  <div class="p-category__item"><a href="/author/zzz">作者頁</a></div>
</div>
<div class="footer"><a href="/article/999">頁尾的文章</a></div>
</body></html>"""


def test_a_mirrormedia_style_section_page():
    out = links(MIRROR, root_selector="main", include_patterns=[r"/story/"], exclude_patterns=[r"/video/"],
                allowed_hosts=["cdn.mirrormedia.example"])
    urls = [(i["id"], i["order"], i["url"], i["title"]) for i in out["items"]]
    base = "https://www.mirrormedia.example"
    assert urls == [
        ("l1", 1, f"{base}/story/aaa111?id=7", "第一則新聞標題"),
        ("l2", 2, f"{base}/story/bbb222", "第二則 新聞標題"),
        ("l3", 3, "https://cdn.mirrormedia.example/story/ddd444", "允許網域的連結"),
        ("l4", 4, f"{base}/story/fff666", "圖片替代文字"),
        ("l5", 5, f"{base}/story/ggg777", "以標籤命名"),
        ("l6", 6, f"{base}/story/hhh888", "以 title 命名"),
    ]


def test_a_ctwant_style_category_page_with_a_root_selector():
    out = links(CTWANT, base_url="https://www.ctwant.example/category/politics", root_selector=".p-category__list",
                include_patterns=[r"^https://www\.ctwant\.example/article/\d+$"])
    assert [(i["url"], i["title"]) for i in out["items"]] == [
        ("https://www.ctwant.example/article/101", "CTWANT 第一篇"),
        ("https://www.ctwant.example/article/102", "CTWANT 第二篇"),
    ]
    whole = links(CTWANT, base_url="https://www.ctwant.example/category/politics", include_patterns=[r"/article/"])
    assert len(whole["items"]) == 3  # without a root_selector the footer link counts


def test_relative_urls_resolve_against_the_base_path():
    html = "<a href='../story/rel1'>up</a><a href='sibling'>sib</a><a href='//www.mirrormedia.example/story/sl'>scheme-relative</a>"
    out = links(html, base_url="https://www.mirrormedia.example/section/politics/", include_patterns=[r"."])
    assert [i["url"] for i in out["items"]] == [
        "https://www.mirrormedia.example/section/story/rel1",
        "https://www.mirrormedia.example/section/politics/sibling",
        "https://www.mirrormedia.example/story/sl",
    ]


def test_the_host_filter_drops_other_hosts_and_is_case_and_port_blind():
    html = "<a href='https://OTHER.example.org/a'>other</a><a href='https://WWW.Mirrormedia.example:8443/b'>same</a>"
    out = links(html)
    assert [i["url"] for i in out["items"]] == ["https://www.mirrormedia.example:8443/b"]
    allowed = links(html, allowed_hosts=["other.example.org"])
    assert len(allowed["items"]) == 2


def test_tracking_keys_fragment_and_trailing_slash_are_dropped_other_keys_kept():
    html = "<a href='/p/1/?UTM_source=a&page=2&fbclid=1#x'>one</a><a href='/p/1?page=2'>again</a>"
    out = links(html)
    assert [i["url"] for i in out["items"]] == ["https://www.mirrormedia.example/p/1?page=2"]


def test_limit_and_page_order():
    html = "".join(f"<a href='/n/{i}'>item {i}</a>" for i in range(100))
    assert len(links(html)["items"]) == 20
    assert [i["title"] for i in links(html, limit=3)["items"]] == ["item 0", "item 1", "item 2"]
    assert len(links(html, limit=60)["items"]) == 60


def test_titles_are_cut_at_200_graphemes():
    html = f"<a href='/n/1'>{'字' * 500}</a>"
    assert len(links(html)["items"][0]["title"]) == 200


def test_include_and_exclude_patterns_see_the_normalised_url():
    html = "<a href='/a/1'>a</a><a href='/b/2'>b</a><a href='/a/3/skip'>c</a>"
    out = links(html, include_patterns=[r"/a/", r"/b/"], exclude_patterns=[r"/skip$", r"/b/"])
    assert [i["url"].rsplit("/", 1)[-1] for i in out["items"]] == ["1"]


# ---------------------------------------------------------------------------- exclude_urls / exclude_terms

LISTING = (
    "<a href='/n/1'>Alpha</a><a href='/n/2/'>Beta</a><a href='/n/3'>美通社：某公司發布</a>"
    "<a href='/n/4'>Delta 新聞稿</a><a href='/n/5'>PR Newswire release</a><a href='/n/6'>Zeta</a>"
    "<a href='http://www.mirrormedia.example/n/7'>plain http</a><a href='https://other.example/n/8'>other host</a>"
    "<a href='/x/9'>off pattern</a><a href='/n/1#top'>Alpha again</a><a href='/n/10'> </a>"
)
INC = {"include_patterns": [r"/n/"]}


def test_exclude_urls_compare_by_the_normalised_url():
    out = links(LISTING, exclude_urls=[
        "https://WWW.mirrormedia.example/n/1/?utm_source=x&fbclid=y#frag",  # utm, fbclid, fragment, case, slash
        "https://www.mirrormedia.example/n/2",  # the page links /n/2/ with a slash
    ], **INC)
    urls = [i["url"].rsplit("/", 1)[-1] for i in out["items"]]
    assert "1" not in urls and "2" not in urls and urls[:2] == ["3", "4"]
    assert out["dropped"]["excluded_url"] == 2
    assert out["dropped"]["duplicate"] == 1  # /n/1#top after /n/1


def test_exclude_urls_that_are_blank_or_not_urls_are_ignored():
    out = links(LISTING, exclude_urls=["", "  ", "not a url", "http://[bad"], **INC)
    assert out["dropped"]["excluded_url"] == 0 and len(out["items"]) == 6


def test_exclude_terms_are_case_insensitive_substrings_of_the_title_and_work_on_cjk():
    out = links(LISTING, exclude_terms=["美通社", "新聞稿", "pr newswire"], **INC)
    assert [i["title"] for i in out["items"]] == ["Alpha", "Beta", "Zeta"]
    assert out["dropped"]["excluded_term"] == 3
    out = links("<a href='/n/1'>Breaking NEWS</a><a href='/n/2'>Other</a>", exclude_terms=["news"], **INC)
    assert [i["title"] for i in out["items"]] == ["Other"]


def test_empty_terms_match_nothing_and_the_term_is_not_searched_in_the_url():
    out = links("<a href='/n/secret'>Title</a>", exclude_terms=["", "secret"], **INC)
    assert [i["title"] for i in out["items"]] == ["Title"]


def test_limit_counts_the_kept_links_not_the_excluded_ones():
    html = "".join(f"<a href='/n/{i}'>item {i}</a>" for i in range(10))
    out = links(html, limit=3, exclude_urls=["https://www.mirrormedia.example/n/0", "https://www.mirrormedia.example/n/1"], exclude_terms=["item 3"], **INC)
    assert [i["title"] for i in out["items"]] == ["item 2", "item 4", "item 5"]
    assert [i["order"] for i in out["items"]] == [1, 2, 3] and [i["id"] for i in out["items"]] == ["l1", "l2", "l3"]
    assert out["dropped"]["excluded_url"] == 2 and out["dropped"]["excluded_term"] == 1


def test_dropped_counters_cover_every_reason():
    out = links(LISTING, exclude_urls=["https://www.mirrormedia.example/n/6"], exclude_terms=["新聞稿"], **INC)
    assert out["dropped"] == {"scheme": 1, "host": 1, "duplicate": 1, "pattern": 1, "no_title": 1, "excluded_url": 1, "excluded_term": 1}
    assert [i["title"] for i in out["items"]] == ["Alpha", "Beta", "美通社：某公司發布", "PR Newswire release"]
    assert links("<a href='/n/1'>a</a>", **INC)["dropped"] == dict.fromkeys(out["dropped"], 0)


def test_exclusions_do_not_change_article_mode():
    out = article(ARTICLE, exclude_urls=["https://x.example/"], exclude_terms=["政策"])
    assert "dropped" not in out and out["title"] == "市府宣布新政策"


def test_the_list_size_caps_are_in_the_input_model_and_the_declaration():
    base = {"html": "x", "base_url": "https://a.example", "mode": "links"}
    HtmlExtractInput(**base, exclude_urls=["u"] * 200, exclude_terms=["t"] * 50)
    with pytest.raises(ValidationError):
        HtmlExtractInput(**base, exclude_urls=["u"] * 201)
    with pytest.raises(ValidationError):
        HtmlExtractInput(**base, exclude_terms=["t"] * 51)
    text = TOOL_DECLARATIONS["html.extract"].description
    assert "exclude_urls" in text and "exclude_terms" in text and "getAuthorFeed" in text and "excluded_term" in text
    assert "exclude_urls" in TOOL_DECLARATIONS["http.request"].description.split("extract_html")[1].split("parse_feed")[0]


# ---------------------------------------------------------------------------- errors


def test_error_codes():
    assert article("<p>x</p>" + " " * (2 * 1024 * 1024))["error"]["code"] == "html_too_large"
    assert article("<p>" + "字" * 800_000 + "</p>")["error"]["code"] == "html_too_large"  # 2.4 MB of UTF-8, 0.8 M characters
    assert article("")["error"]["code"] == "html_unparseable"
    assert article("just words, no markup")["error"]["code"] == "html_unparseable"
    assert links("<a href='/a'>a</a>", include_patterns=[])["error"]["code"] == "no_patterns"
    assert links("<a href='/a'>a</a>", include_patterns=None)["error"]["code"] == "no_patterns"
    assert links("<a href='/a'>a</a>", include_patterns=["("])["error"]["code"] == "invalid_pattern"
    assert links("<a href='/a'>a</a>", root_selector="div[[")["error"]["code"] == "selector_invalid"
    assert article("<p>x</p>", base_url="http://a.example/")["error"]["code"] == "base_url_invalid"


def test_the_cap_is_inclusive_of_exactly_two_mib():
    page = "<p>x</p>"
    page += " " * (2 * 1024 * 1024 - len(page))
    assert "error" not in article(page)


def test_a_pathological_pattern_is_linear_time():
    html = "<a href='/" + "a" * 3000 + "'>t</a>"
    assert links(html, include_patterns=[r"^(a+)+$", r"(x+x+)+y"])["items"] == []


# ---------------------------------------------------------------------------- declaration


def test_the_declaration_is_pure_with_an_example_and_a_result_schema():
    decl = TOOL_DECLARATIONS["html.extract"]
    assert decl.side_effect is False and decl.example and decl.result_schema
    HtmlExtractInput.model_validate({k: v for k, v in decl.example.items()})
    with pytest.raises(ValidationError):
        HtmlExtractInput(html="x", base_url="https://a.example", mode="links", limit=61)
    with pytest.raises(ValidationError):
        HtmlExtractInput(html="x", base_url="https://a.example", mode="text")


# ---------------------------------------------------------------------------- hardening

import time  # noqa: E402


def test_an_xxe_document_reads_no_file_and_loads_no_url(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT")
    html = (
        f'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file://{secret}">'
        f'<!ENTITY remote SYSTEM "https://169.254.169.254/latest">]>'
        "<html><body><article><p>Before &xxe; and &remote; after, with enough words to be a paragraph.</p></article></body></html>"
    )
    out = article(html)
    assert "TOP-SECRET-CONTENT" not in str(out) and "169.254" not in str(out)
    assert "Before" in out["text"]


def test_a_billion_laughs_document_is_bounded():
    lol = "".join(f'<!ENTITY lol{i} "&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};">' for i in range(1, 10))
    html = f'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol0 "lol">{lol}]><html><body><p>&lol9; padding to make a paragraph.</p></body></html>'
    started = time.monotonic()
    out = article(html)
    assert time.monotonic() - started < 2 and len(str(out)) < 10_000
    assert "lollollol" not in out["text"]


def test_a_two_mib_deeply_nested_document_returns_quickly():
    depth = 2 * 1024 * 1024 // 11
    html = "<div>" * depth
    assert len(html.encode()) <= 2 * 1024 * 1024
    for call in (lambda: article(html), lambda: links(html)):
        started = time.monotonic()
        out = call()
        assert time.monotonic() - started < 2
        assert "error" in out or out == {"items": []}


def test_too_many_elements_is_html_too_complex():
    html = "<p>x</p>" * 60_000
    assert len(html) < 2 * 1024 * 1024
    assert article(html)["error"]["code"] == "html_too_complex"
    assert links(html)["error"]["code"] == "html_too_complex"
    assert "error" not in article("<p>x</p>" * 1000)


def test_nesting_beyond_the_depth_cap_is_html_too_complex():
    assert article("<div>" * 400 + "x")["error"]["code"] == "html_too_complex"


def test_pattern_limits():
    assert links("<a href='/a'>a</a>", include_patterns=["a"] * 11)["error"]["code"] == "invalid_pattern"
    assert links("<a href='/a'>a</a>", include_patterns=["a" * 201])["error"]["code"] == "invalid_pattern"
    assert links("<a href='/a'>a</a>", include_patterns=["a"] * 10)["items"] == [
        {"id": "l1", "url": "https://www.mirrormedia.example/a", "title": "a", "order": 1}
    ]
    assert links("<a href='/a'>a</a>", include_patterns=["."], exclude_patterns=["(?=x)"])["error"]["code"] == "invalid_pattern"  # RE2: no look-around
