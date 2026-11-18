"""`feed.parse` (eap_worker.feed_tool): pure RSS 2.0 / Atom parsing, filtering and numbering. No I/O."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, FeedParseInput
from eap_worker.feed_tool import feed_parse

NOW = "2026-11-18T12:00:00Z"

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Entertainment</title>
<item><title>Star A announces a tour</title><link>https://news.example.com/a</link>
 <description><![CDATA[<p>Star A will tour in <b>2027</b>.&nbsp;Tickets&amp;more.</p><img src="x.jpg"/>]]></description>
 <pubDate>Wed, 18 Nov 2026 10:30:00 +0800</pubDate></item>
<item><title>Star B new film</title><link>https://news.example.com/b</link>
 <description>&lt;p&gt;Film &lt;i&gt;B&lt;/i&gt; opens soon&lt;/p&gt;</description>
 <pubDate>Wed, 18 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>Undated story</title><link>https://news.example.com/c</link><description>No date here</description></item>
<item><title>Old story</title><link>https://news.example.com/d</link><description>Very old</description>
 <pubDate>Mon, 01 Jun 2026 00:00:00 GMT</pubDate></item>
<item><title>No link story</title><description>broken</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom feed</title>
<entry><title>Entry one</title><link rel="self" href="https://x.example.com/feed/1"/><link rel="alternate" href="https://x.example.com/1"/>
 <summary type="html">&lt;p&gt;First &lt;b&gt;entry&lt;/b&gt;&lt;/p&gt;</summary><published>2026-11-18T11:00:00Z</published><updated>2026-11-18T11:30:00Z</updated></entry>
<entry><title>Entry two</title><link href="https://x.example.com/2"/>
 <content type="html">Second entry body</content><updated>2026-11-18T08:00:00+08:00</updated></entry>
</feed>"""


def parse(text: str, **kw):
    kw.setdefault("now", NOW)
    return feed_parse(None, text=text, **kw)


def test_rss_items_are_cleaned_numbered_and_newest_first():
    out = parse(RSS)
    assert [i["id"] for i in out["items"]] == ["c1", "c2", "c3", "c4"]
    # newest first, undated last (the old one is dated, so it comes before the undated item)
    assert [i["link"] for i in out["items"]] == [
        "https://news.example.com/b", "https://news.example.com/a", "https://news.example.com/d",
        "https://news.example.com/c"]
    a = out["items"][1]
    assert a["title"] == "Star A announces a tour"
    assert a["summary"] == "Star A will tour in 2027. Tickets&more."
    assert a["published_at"] == "2026-11-18T02:30:00Z"
    assert out["items"][0]["summary"] == "Film B opens soon"  # entity-escaped HTML is stripped as well
    assert out["items"][3]["published_at"] is None
    assert out["count"] == 4 and out["dropped"] == {"excluded_url": 0, "old": 0, "term": 0, "malformed": 1}
    assert out["listing"].splitlines()[1] == "c2｜Star A announces a tour｜Star A will tour in 2027. Tickets&more."


def test_atom_entries_use_the_alternate_link_and_published_or_updated():
    out = parse(ATOM)
    assert [(i["id"], i["link"], i["summary"], i["published_at"]) for i in out["items"]] == [
        ("c1", "https://x.example.com/1", "First entry", "2026-11-18T11:00:00Z"),
        ("c2", "https://x.example.com/2", "Second entry body", "2026-11-18T00:00:00Z"),
    ]


def test_max_age_hours_drops_old_items_but_keeps_undated_ones():
    out = parse(RSS, max_age_hours=10)
    assert [i["link"][-1] for i in out["items"]] == ["b", "a", "c"]
    assert out["dropped"]["old"] == 1 and out["dropped"]["malformed"] == 1


def test_exclude_urls_and_terms():
    out = parse(RSS, exclude_urls=["https://news.example.com/a"], exclude_terms=["FILM", "very old"])
    assert [i["link"][-1] for i in out["items"]] == ["c"]
    assert out["dropped"] == {"excluded_url": 1, "old": 0, "term": 2, "malformed": 1}
    assert [i["id"] for i in out["items"]] == ["c1"]  # numbering follows what is left


def test_exclude_terms_look_at_the_title_and_the_summary():
    out = parse(RSS, exclude_terms=["tickets"])  # only in the summary of item a
    assert "a" not in [i["link"][-1] for i in out["items"]] and out["dropped"]["term"] == 1


def test_max_items_cuts_after_filtering_and_defaults_to_20():
    assert [i["id"] for i in parse(RSS, max_items=2)["items"]] == ["c1", "c2"]
    many = "<rss><channel>" + "".join(
        f"<item><title>T{n}</title><link>https://e.com/{n}</link><pubDate>Wed, 18 Nov 2026 {n % 24:02d}:00:00 GMT</pubDate></item>"
        for n in range(30)) + "</channel></rss>"
    assert parse(many)["count"] == 20


def test_summary_is_cut_at_400_and_the_listing_excerpt_at_120():
    long = "字" * 500
    out = parse(f"<rss><channel><item><title>T</title><link>https://e.com/1</link><description>{long}</description></item></channel></rss>")
    assert len(out["items"][0]["summary"]) == 400
    assert out["listing"] == "c1｜T｜" + "字" * 120


def test_without_now_the_clock_is_used():
    old = "Mon, 01 Jun 2020 00:00:00 GMT"
    xml = f"<rss><channel><item><title>T</title><link>https://e.com/1</link><pubDate>{old}</pubDate></item></channel></rss>"
    assert feed_parse(None, text=xml, max_age_hours=24)["count"] == 0


@pytest.mark.parametrize("text", ["", "not xml at all", "<html><body>hi</body></html>", "<rss><channel>", "<feed></rss>"])
def test_unparseable_or_foreign_documents_are_feed_invalid(text):
    assert parse(text)["error"]["code"] == "feed_invalid"


def test_an_xml_bomb_and_external_entities_are_rejected():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]><rss><channel><item><title>&lol3;</title>'
            '<link>https://e.com/1</link></item></channel></rss>')
    assert parse(bomb)["error"]["code"] == "feed_invalid"
    xxe = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item>'
           '<title>&x;</title><link>https://e.com/1</link></item></channel></rss>')
    assert parse(xxe)["error"]["code"] == "feed_invalid"


def test_a_bad_now_is_reported():
    assert parse(RSS, now="yesterday")["error"]["code"] == "now_invalid"


def test_input_limits():
    for bad in ({"max_items": 0}, {"max_items": 51}, {"max_age_hours": 0}, {"exclude_urls": ["u"] * 201},
                {"exclude_terms": ["t"] * 201}, {"text": "x" * (1024 * 1024 + 1)}):
        with pytest.raises(ValidationError):
            FeedParseInput(**{"text": "<rss/>", **bad})
    assert TOOL_DECLARATIONS["feed.parse"].side_effect is False
