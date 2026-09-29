import asyncio
import os
import time
from pathlib import Path

from revv.cache import DiskCache
from revv.demo import DEMO_REF, DemoBackend
from revv.models import PRRef, RepoRef
from revv.session import ReviewSession
from revv.ui.app import RevvApp
from revv.ui.inbox import InboxScreen
from revv.ui.review import ReviewScreen


def test_pull_request_round_trip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    pr = asyncio.run(DemoBackend(latency=0).load_pull_request(DEMO_REF))
    cache.save_pr(pr)
    loaded = cache.load_pr(DEMO_REF)
    assert loaded is not None
    assert loaded.title == pr.title
    assert [f.path for f in loaded.files] == [f.path for f in pr.files]
    assert len(loaded.threads) == len(pr.threads)
    assert cache.load_pr(PRRef(DEMO_REF.repo, 999)) is None


def test_unreadable_entries_are_ignored(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    path = cache.pr_path(DEMO_REF)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a cache entry")
    assert cache.load_pr(DEMO_REF) is None
    assert not path.exists()  # corrupt entries are removed


def test_blobs_remember_missing_files(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    repo = RepoRef("o", "r")
    assert cache.load_blob(repo, "abc", "a.py") == (False, None)
    cache.save_blob(repo, "abc", "a.py", "print(1)\n")
    cache.save_blob(repo, "abc", "logo.png", None)
    assert cache.load_blob(repo, "abc", "a.py") == (True, "print(1)\n")
    assert cache.load_blob(repo, "abc", "logo.png") == (True, None)


def test_cache_files_are_private(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    cache.save_blob(RepoRef("o", "r"), "abc", "a.py", "x")
    path = cache.blob_path(RepoRef("o", "r"), "abc", "a.py")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_prune_drops_old_entries(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    repo = RepoRef("o", "r")
    cache.save_blob(repo, "old", "a.py", "x")
    cache.save_blob(repo, "new", "a.py", "y")
    old = cache.blob_path(repo, "old", "a.py")
    stale = time.time() - 60 * 86400
    os.utime(old, (stale, stale))
    cache.prune()
    assert not old.exists()
    assert cache.blob_path(repo, "new", "a.py").exists()


def test_session_opens_from_cache_then_syncs(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)

    async def scenario() -> None:
        backend = DemoBackend(latency=0)
        first = ReviewSession(backend, DEMO_REF, cache=cache)
        await first.load()
        assert first.fresh
        await asyncio.sleep(0.05)  # the save happens off-thread
        second = ReviewSession(backend, DEMO_REF, cache=cache)
        pr = await second.load()
        assert not second.fresh  # came from disk
        assert pr.title == first.pr.title
        await second.refresh()
        assert second.fresh
        texts = await second.new_texts(pr.files)
        await asyncio.sleep(0.05)
        third = ReviewSession(DemoBackend(latency=0), DEMO_REF, cache=cache)
        await third.load()
        cached = await asyncio.to_thread(
            third._cached_blobs, [(pr.head_oid, "src/netkit/retry.py")]
        )
        assert cached[(pr.head_oid, "src/netkit/retry.py")] == texts["src/netkit/retry.py"]

    asyncio.run(scenario())


async def test_cached_review_screen_syncs_and_allows_writes(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    pr = await DemoBackend(latency=0).load_pull_request(DEMO_REF)
    pr.title = "Stale title from the cache"
    cache.save_pr(pr)
    app = RevvApp(DemoBackend(latency=0.05), target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
    async with app.run_test(size=(140, 45)) as pilot:
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, ReviewScreen) and screen.session.loaded:
                break
            await pilot.pause(0.01)
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        assert screen.pr.title == "Stale title from the cache"
        banner = screen.query_one("#banner")
        assert banner.display and "cached" in str(banner.render())
        for _ in range(100):
            if screen.session.fresh:
                break
            await pilot.pause(0.02)
        await pilot.pause(0.05)
        assert screen.session.fresh
        assert screen.pr.title.startswith("Retry failed requests")
        assert not banner.display


async def test_inbox_shows_cached_rows_first(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    backend = DemoBackend(latency=0)
    rows = await backend.search_pull_requests("review-requested:@me")
    for row in rows:
        row.details_loaded = True
    cache.save_inbox("github.com", DEMO_REF.repo.full_name, {"requested": rows[:1]})
    app = RevvApp(DemoBackend(latency=0.3), repo=DEMO_REF.repo, cache=cache)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.05)
        inbox = app.screen
        assert isinstance(inbox, InboxScreen)
        requested = next(s for s in inbox.sections if s.key == "requested")
        assert requested.loaded and len(requested.items) == 1  # from the cache, before the network
        for _ in range(100):
            if len(requested.items) == 3:
                break
            await pilot.pause(0.02)
        assert len(requested.items) == 5


def test_entries_from_another_schema_are_ignored(tmp_path: Path) -> None:
    import pickle
    import zlib

    cache = DiskCache(tmp_path)
    path = cache.inbox_path("github.com", "acme/netkit")
    path.parent.mkdir(parents=True)
    # what an older revv wrote: a different format tag
    path.write_bytes(zlib.compress(pickle.dumps((3, {"requested": []}))))
    assert cache.load_inbox("github.com", "acme/netkit") is None


def test_schema_fingerprint_tracks_model_fields() -> None:
    from revv import cache as cache_module

    assert f"4-{cache_module._schema()}" == cache_module.FORMAT
    assert len(cache_module._schema()) == 16


async def test_leaving_while_syncing_does_not_crash(tmp_path: Path) -> None:
    """Closing a pull request while its background sync is running used to crash
    ("No nodes match PRHeader"): the cancelled sync's cleanup looked for the header."""
    cache = DiskCache(tmp_path)
    cache.save_pr(await DemoBackend(latency=0).load_pull_request(DEMO_REF))
    app = RevvApp(DemoBackend(latency=0.5), repo=DEMO_REF.repo, cache=cache)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        app.open_review(DEMO_REF, from_inbox=True)
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, ReviewScreen) and not screen.session.fresh  # syncing
        await pilot.press("q")
        await pilot.pause(1.0)
        assert isinstance(app.screen, InboxScreen)
