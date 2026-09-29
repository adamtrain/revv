"""Review session: the loaded pull request plus every action a reviewer can take on it.

The session owns the `PullRequest` model and keeps it in sync with the backend. Actions
update the local model from the API's response, so the UI can simply re-render.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from revv.backend import Backend
from revv.cache import DiskCache
from revv.models import (
    ChangedFile,
    Comment,
    FileStatus,
    PRRef,
    PullRequest,
    Reaction,
    Review,
    ReviewEvent,
    ReviewThread,
    Side,
    ViewedState,
)


class ReviewSession:
    def __init__(self, backend: Backend, ref: PRRef, cache: DiskCache | None = None) -> None:
        self.backend = backend
        self.ref = ref
        self.cache = cache
        self.fresh = False  # False while showing cached data that hasn't been re-synced yet
        self._pr: PullRequest | None = None
        self._contents: dict[tuple[str, str], str | None] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future[str | None]] = {}
        self._review_lock = asyncio.Lock()

    @property
    def pr(self) -> PullRequest:
        assert self._pr is not None, "pull request not loaded yet"
        return self._pr

    @property
    def loaded(self) -> bool:
        return self._pr is not None

    # -- loading -----------------------------------------------------------------

    async def load(self) -> PullRequest:
        """Load the pull request: instantly from the disk cache if we have it (then call
        `refresh` to sync), otherwise from GitHub."""
        if self.cache is not None:
            cached = await asyncio.to_thread(self.cache.load_pr, self.ref)
            if cached is not None:
                self._pr = cached
                self.fresh = False
                return cached
        self._pr = await self.backend.load_pull_request(self.ref)
        self.fresh = True
        self.save()
        return self._pr

    async def refresh(self) -> PullRequest:
        """Reload everything; patches are reused when the head commit hasn't moved."""
        previous = self._pr
        pr = await self.backend.load_pull_request(self.ref, reuse=previous)
        if previous is not None and previous.head_oid != pr.head_oid:
            self._contents.clear()
        self._pr = pr
        self.fresh = True
        self.save()
        return pr

    def save(self) -> None:
        """Write the current state to the disk cache (serialized here, written off-thread)."""
        if self.cache is None or self._pr is None:
            return
        try:
            data = self.cache.encode(self._pr)
        except Exception:
            return
        path = self.cache.pr_path(self.ref)
        cache = self.cache

        def write() -> None:
            cache.write_bytes(path, data, compress=True)

        try:
            asyncio.get_running_loop().run_in_executor(None, write)
        except RuntimeError:
            write()

    # -- file contents -------------------------------------------------------------

    def cached_new_text(self, file: ChangedFile) -> str | None:
        return self._contents.get((self.pr.head_oid, file.path))

    def has_new_text(self, file: ChangedFile) -> bool:
        return (self.pr.head_oid, file.path) in self._contents

    async def new_texts(self, files: Iterable[ChangedFile]) -> dict[str, str | None]:
        """Full text of files at the head commit, fetched in batches and cached."""
        pr = self.pr
        result: dict[str, str | None] = {}
        wanted: list[tuple[str, str]] = []
        waiting: list[tuple[str, asyncio.Future[str | None]]] = []
        for file in files:
            key = (pr.head_oid, file.path)
            if file.status == FileStatus.REMOVED:
                result[file.path] = None
            elif key in self._contents:
                result[file.path] = self._contents[key]
            elif key in self._inflight:
                waiting.append((file.path, self._inflight[key]))
            else:
                wanted.append(key)
        if wanted and self.cache is not None:
            found = await asyncio.to_thread(self._cached_blobs, wanted)
            for key, text in found.items():
                self._contents[key] = text
                result[key[1]] = text
            wanted = [key for key in wanted if key not in found]
        if wanted:
            loop = asyncio.get_running_loop()
            futures = {key: loop.create_future() for key in wanted}
            self._inflight.update(futures)
            try:
                fetched = await self.backend.file_contents(pr, wanted)
            except Exception as error:
                for key, future in futures.items():
                    self._inflight.pop(key, None)
                    future.set_exception(error)
                    future.exception()  # mark retrieved
                raise
            for key, future in futures.items():
                text = fetched.get(key)
                self._contents[key] = text
                self._inflight.pop(key, None)
                future.set_result(text)
                result[key[1]] = text
            if self.cache is not None:
                cache, repo = self.cache, pr.ref.repo
                items = [(key, fetched.get(key)) for key in futures]

                def store() -> None:
                    for (oid, path), text in items:
                        cache.save_blob(repo, oid, path, text)

                asyncio.get_running_loop().run_in_executor(None, store)
        for path, future in waiting:
            try:
                result[path] = await future
            except Exception:
                result[path] = None
        return result

    # -- reviews -----------------------------------------------------------------

    @property
    def pending_review(self) -> Review | None:
        return self.pr.pending_review

    async def _ensure_review(self) -> str:
        async with self._review_lock:
            pending = self.pr.pending_review
            if pending is not None:
                return pending.id
            review = await self.backend.start_review(self.pr)
            self.pr.reviews.append(review)
            return review.id

    def _replace_review(self, review: Review) -> None:
        reviews = self.pr.reviews
        for index, existing in enumerate(reviews):
            if existing.id == review.id:
                reviews[index] = review
                return
        reviews.append(review)

    def _publish_pending(self, review_id: str) -> None:
        for thread in self.pr.threads:
            changed = False
            for comment in thread.comments:
                if comment.is_pending and comment.review_id in (review_id, None):
                    comment.is_pending = False
                    changed = True
            if changed:
                thread.touch()

    async def submit_review(self, event: ReviewEvent, body: str) -> Review:
        pending = self.pr.pending_review
        review = await self.backend.submit_review(
            self.pr, pending.id if pending else None, event, body
        )
        self._replace_review(review)
        if pending is not None:
            self._publish_pending(pending.id)
        return review

    async def discard_pending_review(self) -> None:
        pending = self.pr.pending_review
        if pending is None:
            return
        await self.backend.delete_review(pending.id)
        self.pr.reviews = [r for r in self.pr.reviews if r.id != pending.id]
        threads = []
        for thread in self.pr.threads:
            kept = [c for c in thread.comments if not c.is_pending]
            if kept:
                if len(kept) != len(thread.comments):
                    thread.comments = kept
                    thread.touch()
                threads.append(thread)
        self.pr.threads = threads

    # -- review threads ----------------------------------------------------------

    async def add_thread(
        self,
        *,
        path: str,
        body: str,
        line: int | None = None,
        side: Side | None = None,
        start_line: int | None = None,
        start_side: Side | None = None,
        file_level: bool = False,
        publish_now: bool = False,
    ) -> ReviewThread:
        """Add a new comment thread. `publish_now` posts it immediately as a one-off review,
        which is only possible when there is no pending review (as on github.com)."""
        publish_now = publish_now and self.pr.pending_review is None
        review_id = await self._ensure_review()
        thread = await self.backend.add_thread(
            self.pr,
            review_id,
            path=path,
            body=body,
            line=line,
            side=side,
            start_line=start_line,
            start_side=start_side,
            file_level=file_level,
        )
        self.pr.threads.append(thread)
        if publish_now:
            review = await self.backend.submit_review(self.pr, review_id, ReviewEvent.COMMENT, "")
            self._replace_review(review)
            self._publish_pending(review_id)
        return thread

    async def reply(self, thread: ReviewThread, body: str, publish_now: bool = False) -> Comment:
        publish_now = publish_now and self.pr.pending_review is None
        review_id = await self._ensure_review()
        comment = await self.backend.add_reply(review_id, thread.id, body)
        thread.comments.append(comment)
        thread.touch()
        if publish_now:
            review = await self.backend.submit_review(self.pr, review_id, ReviewEvent.COMMENT, "")
            self._replace_review(review)
            self._publish_pending(review_id)
        return comment

    def thread_of(self, comment: Comment) -> ReviewThread | None:
        for thread in self.pr.threads:
            if any(c.id == comment.id for c in thread.comments):
                return thread
        return None

    async def edit_comment(self, comment: Comment, body: str) -> Comment:
        if comment.is_review_comment:
            updated = await self.backend.update_review_comment(comment.id, body)
            thread = self.thread_of(comment)
            if thread is not None:
                thread.comments = [updated if c.id == comment.id else c for c in thread.comments]
                thread.touch()
        else:
            updated = await self.backend.update_issue_comment(comment.id, body)
            self.pr.comments = [updated if c.id == comment.id else c for c in self.pr.comments]
        return updated

    async def delete_comment(self, comment: Comment) -> None:
        if comment.is_review_comment:
            await self.backend.delete_review_comment(comment.id)
            thread = self.thread_of(comment)
            if thread is not None:
                thread.comments = [c for c in thread.comments if c.id != comment.id]
                thread.touch()
                if not thread.comments:
                    self.pr.threads = [t for t in self.pr.threads if t is not thread]
        else:
            await self.backend.delete_issue_comment(comment.id)
            self.pr.comments = [c for c in self.pr.comments if c.id != comment.id]

    async def set_resolved(self, thread: ReviewThread, resolved: bool) -> None:
        before = (thread.is_resolved, thread.resolved_by)
        thread.is_resolved = resolved
        thread.resolved_by = self.pr.viewer_login if resolved else None
        thread.touch()
        try:
            (
                thread.is_resolved,
                thread.resolved_by,
                thread.viewer_can_resolve,
                thread.viewer_can_unresolve,
            ) = await self.backend.set_thread_resolved(thread.id, resolved)
        except Exception:
            thread.is_resolved, thread.resolved_by = before
            raise
        finally:
            thread.touch()

    # -- conversation --------------------------------------------------------------

    async def add_issue_comment(self, body: str) -> Comment:
        comment = await self.backend.add_issue_comment(self.pr, body)
        self.pr.comments.append(comment)
        return comment

    async def set_comment_resolved(self, item: Comment | Review, resolved: bool) -> None:
        """Resolve a general comment (or a review summary) by minimizing it as RESOLVED."""
        before = (item.is_minimized, item.minimized_reason)
        item.is_minimized, item.minimized_reason = resolved, "RESOLVED" if resolved else None
        try:
            minimized, reason = await self.backend.set_minimized(item.id, resolved)
        except Exception:
            item.is_minimized, item.minimized_reason = before
            raise
        item.is_minimized = minimized
        item.minimized_reason = reason if minimized else None

    async def set_viewed(self, file: ChangedFile, viewed: bool) -> None:
        before = file.viewed
        file.viewed = ViewedState.VIEWED if viewed else ViewedState.UNVIEWED
        try:
            await self.backend.set_viewed(self.pr, file.path, viewed)
        except Exception:
            file.viewed = before
            raise

    async def set_viewed_many(self, files: list[ChangedFile], viewed: bool) -> None:
        if not files:
            return
        before = [(file, file.viewed) for file in files]
        for file in files:
            file.viewed = ViewedState.VIEWED if viewed else ViewedState.UNVIEWED
        try:
            await self.backend.set_viewed_many(self.pr, [f.path for f in files], viewed)
        except Exception:
            for file, state in before:
                file.viewed = state
            raise

    def _cached_blobs(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str | None]:
        assert self.cache is not None
        found: dict[tuple[str, str], str | None] = {}
        for oid, path in keys:
            hit, text = self.cache.load_blob(self.ref.repo, oid, path)
            if hit:
                found[(oid, path)] = text
        return found

    async def text_at_head(self, path: str) -> str | None:
        """Any file of the repository at the head commit (e.g. .gitattributes)."""
        key = (self.pr.head_oid, path)
        if key in self._contents:
            return self._contents[key]
        if self.cache is not None:
            hit, text = await asyncio.to_thread(self.cache.load_blob, self.ref.repo, *key)
            if hit:
                self._contents[key] = text
                return text
        fetched = await self.backend.file_contents(self.pr, [key])
        self._contents[key] = fetched.get(key)
        if self.cache is not None:
            await asyncio.to_thread(self.cache.save_blob, self.ref.repo, *key, fetched.get(key))
        return self._contents[key]

    async def toggle_reaction(self, item: Comment | Review, content: str) -> None:
        reaction = next((r for r in item.reactions if r.content == content), None)
        add = reaction is None or not reaction.viewer_has_reacted
        await self.backend.set_reaction(item.id, content, add)
        if reaction is None:
            item.reactions.append(Reaction(content, 1, True))
        else:
            reaction.count += 1 if add else -1
            reaction.viewer_has_reacted = add
            if reaction.count <= 0:
                item.reactions.remove(reaction)
