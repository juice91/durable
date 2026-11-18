"""The GitHub repository documents adapter against the fake GitHub API: config, connection check, targeted,
full and incremental syncs, the blob-sha ledger, removal and its guard, limits and the token."""

from __future__ import annotations

import asyncio
import json

import pytest

from eap_kb_sources import adapter_for
from eap_kb_sources.errors import (
    CONNECTION_AUTH_FAILED,
    INVALID_DOCUMENT,
    INVALID_REQUEST,
    NOT_FOUND,
    PROVIDER_ERROR,
    RESPONSE_TOO_LARGE,
    SECRET_MISSING,
    SourceError,
)
from eap_kb_sources.providers.github.adapter import GithubRepositoryDocsAdapter, git_blob_sha, path_problem
from eap_kb_sources.providers.github.fixtures import TOKEN, FakeGithub, pdf_bytes, ti_like_repo

from .helpers import CONFIG, REPO, Clock, MemoryStore, Secrets, conn, make_adapter, run_segment, walk_all

DOCS = 30  # md, rst, yaml, pdf under docs/** (the symlink and the oversize file are separate)


def fresh():
    repo = ti_like_repo()
    return repo, FakeGithub(repo), MemoryStore(), None


def api_calls(server, fragment):
    return sum(1 for _h, p, _q, _a in server.requests if fragment in p)


# ------------------------------------------------------------------------------------------ the registry


def test_registered_for_repository_docs_github():
    assert isinstance(adapter_for("repository_docs", "github"), GithubRepositoryDocsAdapter)


# ------------------------------------------------------------------------------------------ validate_config


@pytest.mark.parametrize("config", [
    {"repository": "o/r", "include": ["**/*.md"]},
    {"repository": "o/r", "ref": "v1.2", "include": ["docs/**"], "exclude": ["docs/old/**"], "map": [],
     "max_bytes": 1000, "key_prefix": "gh:", "document_policy": {"types": ["md"], "delete_missing": True}},
    CONFIG,
])
def test_valid_configs(config):
    GithubRepositoryDocsAdapter().validate_config(config)


@pytest.mark.parametrize("config, fragment", [
    ({"include": ["a"]}, "repository"),
    ({"repository": "justname", "include": ["a"]}, "repository"),
    ({"repository": "o/r.git", "include": ["a"]}, "repository"),
    ({"repository": "o/r"}, "include"),
    ({"repository": "o/r", "include": []}, "include"),
    ({"repository": "o/r", "include": ["a"], "exclude": "x"}, "exclude"),
    ({"repository": "o/r", "include": ["a"], "ref": "a..b"}, "ref"),
    ({"repository": "o/r", "include": ["a"], "max_bytes": 0}, "max_bytes"),
    ({"repository": "o/r", "include": ["a"], "max_bytes": 10**9}, "max_bytes"),
    ({"repository": "o/r", "include": ["a"], "document_policy": {"types": ["exe"]}}, "types"),
    ({"repository": "o/r", "include": ["a"], "document_policy": {"delete_missing": "yes"}}, "delete_missing"),
    ({"repository": "o/r", "include": ["a"], "key_prefix": "a\\b"}, "key_prefix"),
    ({"repository": "o/r", "include": ["a"], "map": {"**": {"a": 1}}}, "order"),
    ({"repository": "o/r", "include": ["a"], "map": [{"metadata": {}}]}, "match"),
    ({"repository": "o/r", "include": ["a"], "map": [{"match": "**", "metadata": {"path": "x"}}]}, "reserved"),
    ({"repository": "o/r", "include": ["a"], "map": [{"match": "**", "metadata": {"a": [1]}}]}, "flat"),
    ({"repository": "o/r", "include": ["a"], "map": [{"match": "**", "metadata": {f"k{i}": i for i in range(12)}}]}, "at most"),
])
def test_invalid_configs_are_refused_with_a_short_message(config, fragment):
    with pytest.raises(ValueError, match=fragment):
        GithubRepositoryDocsAdapter().validate_config(config)


# ------------------------------------------------------------------------------------------ check_connection


def check(server, **kw):
    return asyncio.run(make_adapter(server).check_connection(kw.pop("connection", conn()), kw.pop("secrets", Secrets())))


def test_check_ok_with_a_token_and_without():
    server = FakeGithub(ti_like_repo())
    assert check(server).ok
    assert server.requests[-1][3] is True  # the token went out
    assert check(server, connection=conn(token=None)).ok
    assert server.requests[-1][3] is False


def test_check_reports_a_bad_token_and_a_missing_secret_by_code():
    server = FakeGithub(ti_like_repo())
    bad = check(server, secrets=Secrets({"gh-token": "nope"}))
    assert (bad.ok, bad.error_code) == (False, CONNECTION_AUTH_FAILED)
    missing = check(server, secrets=Secrets({}))
    assert (missing.ok, missing.error_code) == (False, SECRET_MISSING)


# ------------------------------------------------------------------------------------------ targeted


def test_targeted_paths_become_documents_with_mapped_metadata():
    repo, server, store, _ = fresh()
    paths = ["docs/api_guide/device/am263x/driver_0.md", "docs/pdf/am64x_trm.pdf", "docs/guides/guide_0.md"]
    result = run_segment(make_adapter(server), store, mode="targeted", parts=paths)
    assert result.complete and result.counters.discovered == 3 and result.counters.documents_queued == 3
    assert result.counters.failed == 0
    key = "source:s1:docs~api_guide~device~am263x~driver_0.md"
    doc = store.docs[key]
    assert doc["metadata"] == {"sdk_family": "mcu-plus-sdk", "device": "AM263x", "path": paths[0]}  # first rule wins
    assert doc["mime"] == "text/markdown" and doc["title"] == paths[0]
    assert store.docs["source:s1:docs~pdf~am64x_trm.pdf"]["mime"] == "application/pdf"
    assert store.docs["source:s1:docs~guides~guide_0.md"]["metadata"] == {"sdk_family": "mcu-plus-sdk", "path": paths[2]}
    assert doc["url"] == f"https://github.com/{REPO}/blob/{repo.head}/docs/api_guide/device/am263x/driver_0.md"
    assert store.entities[REPO]["entity_type"] == "repository"
    assert ("commit", repo.head, False) in store.entities[REPO]["identifiers"]
    assert api_calls(server, "/commits/") == 1 and api_calls(server, "/git/trees") == 0  # no tree walk for 3 paths


def test_targeted_refuses_bad_paths_one_by_one_and_goes_on():
    repo, server, store, _ = fresh()
    repo.put("docs/api_guide/real.md", b"# real\n")
    repo.commit()
    paths = ["../etc/passwd.md", "/abs/x.md", "docs/a/../b.md", "src/main.c", "docs/api_guide/current.md",
             "docs/api_guide/missing.md", "docs/api_guide", "docs/api_guide/real.md"]
    result = run_segment(make_adapter(server), store, mode="targeted", parts=paths)
    assert result.counters.documents_queued == 1 and result.counters.failed == len(paths) - 1
    codes = {e["external_id"]: e["code"] for e in result.errors}
    assert codes["../etc/passwd.md"] == INVALID_REQUEST and codes["/abs/x.md"] == INVALID_REQUEST
    assert codes["src/main.c"] == INVALID_REQUEST  # not in include
    assert codes["docs/api_guide/current.md"] == INVALID_DOCUMENT  # the symlink
    assert codes["docs/api_guide/missing.md"] == NOT_FOUND
    assert codes["docs/api_guide"] == INVALID_REQUEST  # not a document type
    assert list(store.docs) == ["source:s1:docs~api_guide~real.md"]


def test_path_problem_covers_traversal_and_oddities():
    for bad in ("", "..", "a/../b", "/a", "a/", "a//b", "a\\b", "a\nb", "./a", "x" * 2000):
        assert path_problem(bad) is not None
    assert path_problem("docs/a b/c.md") is None


# ------------------------------------------------------------------------------------------ full walk


def test_full_walk_hands_off_every_document_once():
    repo, server, store, _ = fresh()
    final, results = walk_all(make_adapter(server), store)
    assert len(results) == 1 and final.complete
    c = final.counters
    assert (c.discovered, c.inserted, c.documents_queued, c.unchanged) == (DOCS, DOCS, DOCS, 0)
    assert c.failed == 1 and final.errors[0]["code"] == INVALID_DOCUMENT  # the symlink named current.md
    assert len(store.docs) == DOCS
    assert final.source_revision == repo.head
    assert "commit" not in final.next_cursor  # a walk with a failure does not record the head
    rec = store.records[(REPO, "tree")]
    assert rec["commit"] == repo.head and rec["files"] == DOCS + 1 and len(rec["listing_hash"]) == 64
    assert api_calls(server, "/contents/") == DOCS  # one call per file, none for the rest
    assert api_calls(server, "/git/trees") == 1


def test_a_second_full_walk_costs_no_fetch_for_unchanged_blobs():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store)
    server.requests.clear()
    final, _ = walk_all(adapter, store)
    assert final.counters.unchanged == DOCS and final.counters.documents_queued == 0
    assert api_calls(server, "/contents/") == 0 and server.paths("raw.githubusercontent.com") == []


def test_a_changed_blob_is_a_new_version_and_an_unchanged_one_is_not_fetched():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store)
    repo.put("docs/guides/guide_0.md", b"# Guide 0\n\nrewritten\n")
    repo.commit()
    server.requests.clear()
    final, _ = walk_all(adapter, store)
    c = final.counters
    assert (c.updated, c.documents_queued, c.unchanged) == (1, 1, DOCS - 1)
    assert api_calls(server, "/contents/") == 1
    assert store.docs["source:s1:docs~guides~guide_0.md"]["data"].endswith(b"rewritten\n")


def test_a_changed_map_resubmits_the_files_it_touches_without_refetching_unchanged_content_rules():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store)
    config = {"map": [{"match": "**", "metadata": {"sdk_family": "other"}}]}
    final, _ = walk_all(adapter, store, config=config)
    assert final.counters.documents_queued == DOCS  # new metadata = new revision for every file
    assert store.docs["source:s1:docs~guides~guide_1.md"]["metadata"]["sdk_family"] == "other"


def test_segments_continue_from_the_offset_until_the_tree_is_done():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    final, results = walk_all(adapter, store, call_budget=10)
    assert len(results) == 3  # 30 fetches / 10 + the symlink and tail
    first = results[0]
    assert not first.complete and first.next_cursor["commit"] == repo.head and first.next_cursor["offset"] > 0
    assert sum(r.counters.documents_queued for r in results) == DOCS
    assert len(store.docs) == DOCS
    assert api_calls(server, "/commits/") == 1  # later segments resume from the pinned tree


def test_the_oversize_file_is_refused_by_size_before_any_fetch():
    repo, server, store, _ = fresh()
    final, _ = walk_all(make_adapter(server), store, config={"exclude": [], "max_bytes": 100_000})
    assert any(e["code"] == RESPONSE_TOO_LARGE and e["external_id"] == "docs/big/huge.md" for e in final.errors)
    assert "source:s1:docs~big~huge.md" not in store.docs
    assert all("big/huge.md" not in p for p in server.paths("raw.githubusercontent.com"))


def test_a_file_above_one_mib_is_fetched_from_the_raw_host():
    repo, server, store, _ = fresh()
    final, _ = walk_all(make_adapter(server), store, config={"exclude": []})
    assert "source:s1:docs~big~huge.md" in store.docs
    assert server.paths("raw.githubusercontent.com") == [f"/{REPO}/{repo.head}/docs/big/huge.md"]
    assert api_calls(server, "contents/docs/big") == 0


def test_a_blob_that_does_not_match_its_sha_is_refused():
    repo, server, store, _ = fresh()
    sha = git_blob_sha(b"# Guide 0\n\nSteps 0.\n")

    def tampered(request):
        import httpx2
        from base64 import encodebytes
        data = b"# not what the tree said\n"
        return httpx2.Response(200, json={"type": "file", "sha": sha, "size": len(data), "encoding": "base64",
                                          "content": encodebytes(data).decode()})

    server.script(f"/repos/{REPO}/contents/docs/guides/guide_0.md", [tampered])
    final, _ = walk_all(make_adapter(server), store)
    bad = [e for e in final.errors if e.get("external_id") == "docs/guides/guide_0.md"]
    assert bad and bad[0]["code"] == PROVIDER_ERROR
    assert "source:s1:docs~guides~guide_0.md" not in store.docs


def test_a_truncated_tree_falls_back_to_per_directory_listing_pruned_by_include():
    repo, server, store, _ = fresh()
    server.tree_limit = 5
    final, _ = walk_all(make_adapter(server), store)
    assert final.counters.documents_queued == DOCS and final.sweep_ok
    listings = [p for p in server.paths() if "/git/trees/" in p]
    assert len(listings) > 2  # root, docs, and each directory under it
    repo.put("src/extra/deep.md", b"# not included\n")
    repo.commit()
    server.requests.clear()
    walk_all(make_adapter(server), store)
    # `src` can never hold an include match (`docs/**`): its tree is not listed
    src_sha = next(e["sha"] for e in repo.trees[repo.trees[f"commit:{repo.head}"]["tree"]]["entries"] if e["path"] == "src")
    assert not any(src_sha in p for p in server.paths())


def test_an_incomplete_listing_vetoes_the_sweep(monkeypatch):
    import eap_kb_sources.providers.github.adapter as module

    repo, server, store, _ = fresh()
    server.tree_limit = 5
    monkeypatch.setattr(module, "MAX_TREE_CALLS", 2)
    final, _ = walk_all(make_adapter(server), store)
    assert final.sweep_ok is False


# ------------------------------------------------------------------------------------------ incremental


def test_incremental_is_a_no_op_while_the_head_commit_is_unchanged():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    first, _ = walk_all(adapter, store, mode="full", config={"exclude": ["docs/big/**", "**/current.md"]})
    assert first.counters.failed == 0
    server.requests.clear()
    final, results = walk_all(adapter, store, mode="incremental", cursor=first.next_cursor,
                              config={"exclude": ["docs/big/**", "**/current.md"]})
    assert len(results) == 1 and final.complete and final.counters.unchanged == DOCS
    assert final.counters.documents_queued == 0 and final.source_revision == repo.head
    assert [p for p in server.paths() if "/contents/" in p or "/git/trees" in p] == []
    assert api_calls(server, "/commits/") == 1


def test_incremental_walks_when_the_head_moved_and_fetches_only_the_changed_blob():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    cfg = {"exclude": ["docs/big/**", "**/current.md"]}
    first, _ = walk_all(adapter, store, config=cfg)
    repo.put("docs/guides/guide_1.md", b"# changed\n")
    repo.commit()
    server.requests.clear()
    final, _ = walk_all(adapter, store, mode="incremental", cursor=first.next_cursor, config=cfg)
    assert final.counters.updated == 1 and final.counters.unchanged == DOCS - 1
    assert api_calls(server, "/contents/") == 1
    assert final.next_cursor["commit"] == repo.head


def test_incremental_walks_again_when_the_source_config_changed():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    cfg = {"exclude": ["docs/big/**", "**/current.md"]}
    first, _ = walk_all(adapter, store, config=cfg)
    server.requests.clear()
    final, _ = walk_all(adapter, store, mode="incremental", cursor=first.next_cursor,
                        config={**cfg, "include": ["docs/guides/**"]})
    assert api_calls(server, "/git/trees") == 1


def test_a_walk_with_failures_does_not_record_the_head_so_the_next_incremental_retries():
    repo, server, store, _ = fresh()
    final, _ = walk_all(make_adapter(server), store)  # the symlink is a failure
    assert final.counters.failed == 1 and "commit" not in final.next_cursor


# ------------------------------------------------------------------------------------------ removal


def test_a_removed_file_is_marked_removed_and_deleted_only_with_delete_missing():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store)
    repo.remove("docs/guides/guide_0.md")
    repo.remove("docs/guides/guide_1.md")
    repo.commit()
    walk_all(adapter, store)
    assert store.docs["source:s1:docs~guides~guide_0.md"]["status"] == "removed"
    assert store.docs["source:s1:docs~guides~guide_1.md"]["status"] == "removed"
    assert store.kb_deleted == []
    walk_all(adapter, store, config={"document_policy": {"delete_missing": True}})
    assert sorted(store.kb_deleted) == ["source:s1:docs~guides~guide_0.md", "source:s1:docs~guides~guide_1.md"]
    assert "source:s1:docs~guides~guide_0.md" not in store.docs


def test_delete_missing_is_guarded_by_count_and_percent():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store)
    for path in [p for p in list(repo.files) if p.startswith("docs/api_guide/device/am243x")]:
        repo.remove(path)  # 6 of 30 documents = 20 %: the limit is "over", 6 * 100 > 20 * 30 is False
    repo.remove("docs/pdf/am64x_trm.pdf")  # 7 = 23 %
    repo.commit()
    final, _ = walk_all(adapter, store, config={"document_policy": {"delete_missing": True}})
    assert store.kb_deleted == []  # refused
    assert any("delete_missing refused" in e["message"] for e in final.errors)
    assert sum(d["status"] == "removed" for d in store.docs.values()) == 7  # still marked in the ledger


def test_files_that_leave_the_include_scope_count_as_removed_but_other_prefixes_are_untouched():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store)
    store.docs["manual:other"] = {"status": "queued", "source_revision": "x", "url": "u", "metadata": {}, "hash": "h", "data": b""}
    walk_all(adapter, store, config={"exclude": ["docs/big/**", "docs/guides/**"]})
    assert store.docs["source:s1:docs~guides~guide_0.md"]["status"] == "removed"
    assert store.docs["manual:other"]["status"] == "queued"


# ------------------------------------------------------------------------------------------ rate limit


def test_the_rate_limit_stops_a_segment_with_a_cursor_and_the_next_one_waits_for_the_reset():
    repo, server, store, _ = fresh()
    server.limit, server.reset = 12, 5_000
    clock = Clock(1_000)
    adapter = make_adapter(server, clock=clock)
    first = run_segment(adapter, store)
    assert not first.complete and first.counters.documents_queued > 0
    assert first.next_cursor["rate"] == {"remaining": 0, "reset": 5_000}
    assert first.next_cursor["commit"] == repo.head and first.next_cursor["offset"] == first.counters.documents_queued + first.counters.failed
    done_before = len(store.docs)
    server.requests.clear()
    second = run_segment(adapter, store, cursor=first.next_cursor)  # still limited: no call at all
    assert not second.complete and second.calls_used == 0 and server.requests == []
    assert second.counters.discovered == 0 and second.counters.failed == 0  # the worker reads this as "budget"
    clock.now, server.used, server.limit = 5_001, 0, 5000  # the window reset
    final, _ = walk_all(adapter, store, cursor=first.next_cursor)
    assert final.complete and len(store.docs) == DOCS and len(store.docs) > done_before
    assert "rate" not in final.next_cursor


def test_a_403_with_the_limit_spent_is_a_rate_limit_not_an_auth_failure():
    repo, server, store, _ = fresh()
    server.limit = 0
    result = run_segment(make_adapter(server), store)
    assert not result.complete and result.next_cursor["rate"]["remaining"] == 0


# ------------------------------------------------------------------------------------------ the token


def test_a_public_repository_runs_without_a_token_and_the_token_never_leaves_the_step():
    repo, server, store, _ = fresh()
    final, _ = walk_all(make_adapter(server), store, connection=conn(token=None), secrets=Secrets({}))
    assert final.counters.documents_queued == DOCS
    assert all(authorized is False for _h, _p, _q, authorized in server.requests)
    repo2, server2, store2, _ = fresh()
    final2, _ = walk_all(make_adapter(server2), store2)
    assert all(a for _h, p, _q, a in server2.requests if _h == "api.github.com")
    blob = json.dumps([final2.errors, final2.next_cursor, [str(e) for e in final2.errors]], default=str)
    assert TOKEN not in blob
    assert TOKEN not in json.dumps({k: d["metadata"] for k, d in store2.docs.items()})


def test_a_bad_token_ends_the_sync_with_the_attention_code():
    repo, server, store, _ = fresh()
    with pytest.raises(SourceError) as info:
        run_segment(make_adapter(server), store, secrets=Secrets({"gh-token": "wrong"}))
    assert info.value.code == CONNECTION_AUTH_FAILED and "wrong" not in str(info.value)


def test_a_missing_secret_fails_before_any_call():
    repo, server, store, _ = fresh()
    with pytest.raises(SourceError) as info:
        run_segment(make_adapter(server), store, secrets=Secrets({}))
    assert info.value.code == SECRET_MISSING and server.requests == []


# ------------------------------------------------------------------------------------------ hosts


def test_only_the_github_hosts_are_reachable():
    import httpx2

    repo, server, store, _ = fresh()
    server.script(f"/repos/{REPO}/commits/HEAD", [lambda r: httpx2.Response(302, headers={"location": "https://evil.example/x"})])
    with pytest.raises(SourceError) as info:
        run_segment(make_adapter(server), store)
    assert info.value.code == "url_not_allowed"
    assert all(host != "evil.example" for host, *_ in server.requests)


def test_documents_use_the_stable_path_key_not_the_commit_url():
    repo, server, store, _ = fresh()
    adapter = make_adapter(server)
    walk_all(adapter, store, config={"key_prefix": "gh:"})
    assert "gh:docs~guides~guide_0.md" in store.docs
    repo.put("docs/guides/guide_0.md", b"# v2\n")
    repo.commit()
    walk_all(adapter, store, config={"key_prefix": "gh:"})
    assert len([k for k in store.docs if k.startswith("gh:docs~guides~guide_0")]) == 1
    assert store.docs["gh:docs~guides~guide_0.md"]["url"].endswith(f"/blob/{repo.head}/docs/guides/guide_0.md")
