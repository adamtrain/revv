"""Claude's summaries of long pull request descriptions (the API is faked here)."""

from __future__ import annotations

from typing import Any, ClassVar

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage, BetaTextBlock

from revv import summary as summarizer
from revv.cache import DiskCache
from revv.config import save_config
from revv.demo import DEMO_REF, DemoBackend
from revv.models import DescriptionSummary
from revv.ui.app import RevvApp

LONG = "\n\n".join(
    f"Paragraph {i}: this change reworks the retry loop so that requests back off "
    "exponentially, and it explains at length why, how and what was tested."
    for i in range(80)
)
SUMMARY = "**What changed**\nRetries back off exponentially.\n\n**Why**\nFewer outages."


def message(text: str, stop_reason: str = "end_turn") -> BetaMessage:
    block = BetaTextBlock.model_construct(type="text", text=text, citations=None)
    return BetaMessage.model_construct(
        content=[block] if text else [], stop_reason=stop_reason, model=summarizer.MODEL
    )


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("nope", response=httpx2.Response(code, request=request), body=None)


class FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic: records requests, plays back `results`."""

    calls: ClassVar[list[dict[str, Any]]] = []
    results: ClassVar[list[object]] = []

    def __init__(self, **options: Any) -> None:
        self.beta = self
        self.messages = self

    async def __aenter__(self) -> FakeAnthropic:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def create(self, **request: Any) -> BetaMessage:
        FakeAnthropic.calls.append(request)
        result = FakeAnthropic.results.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, BetaMessage)
        return result


@pytest.fixture
def fake_api(monkeypatch) -> type[FakeAnthropic]:
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAnthropic)
    FakeAnthropic.calls, FakeAnthropic.results = [], []
    return FakeAnthropic


def test_only_with_a_key_and_the_setting_on(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not summarizer.available()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert summarizer.available()
    save_config(summaries=False)
    assert not summarizer.available()


async def test_the_request(fake_api) -> None:
    fake_api.results = [message(SUMMARY)]
    result = await summarizer.summarize("Back off exponentially", LONG)
    assert result.summary == SUMMARY and result.error is None and result.text == LONG
    [request] = fake_api.calls
    assert request["model"] == "claude-sonnet-5-5"
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["output_config"] == {"effort": "low"}
    assert "What changed" in request["system"] and "How it was tested" in request["system"]
    assert "ASD-STE100 Simplified Technical English" in request["system"]
    assert "A descriptive sentence has a maximum of 25 words." in request["system"]
    assert "about 300 words and never more than 350" in request["system"]
    content = request["messages"][0]["content"]
    assert "<title>Back off exponentially</title>" in content and LONG in content


async def test_a_brief_summary_is_half_as_long_as_the_description(fake_api) -> None:
    fake_api.results = [message(SUMMARY)]
    await summarizer.summarize("t", "A short description.", brief=True)
    [request] = fake_api.calls
    assert "about half as long as the description" in request["system"]
    assert "300 words" not in request["system"]
    assert "Simplified Technical English" in request["system"]


async def test_without_server_side_fallback_if_it_is_rejected(fake_api) -> None:
    fake_api.results = [status_error(anthropic.BadRequestError, 400), message(SUMMARY)]
    result = await summarizer.summarize("t", LONG)
    assert result.summary == SUMMARY
    assert [
        ("fallbacks" in c and c["fallbacks"] is not anthropic.omit) for c in fake_api.calls
    ] == [
        True,
        False,
    ]


async def test_refusals_and_errors(fake_api) -> None:
    fake_api.results = [message("", stop_reason="refusal")]
    refused = await summarizer.summarize("t", LONG)
    assert refused.refused and refused.error and not refused.summary
    fake_api.results = [status_error(anthropic.AuthenticationError, 401)]
    failed = await summarizer.summarize("t", LONG)
    assert failed.error is not None and "ANTHROPIC_API_KEY" in failed.error
    assert not failed.refused


class Summaries:
    """A fake summarizer for the app: counts the descriptions it's asked about."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.brief: list[bool] = []

    async def __call__(self, title: str, body: str, *, brief: bool = False) -> DescriptionSummary:
        self.asked.append(body)
        self.brief.append(brief)
        return DescriptionSummary(text=body, summary=SUMMARY, model=summarizer.MODEL)


@pytest.fixture
def summaries(monkeypatch) -> Summaries:
    fake = Summaries()
    monkeypatch.setattr(summarizer, "summarize", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return fake


def long_demo(body: str = LONG) -> DemoBackend:
    backend = DemoBackend(latency=0)
    backend._pr.body = body
    return backend


def description_text(screen) -> str:
    from revv.ui.conversation import Card

    card = next(c for c in screen.query(Card) if c.item.kind == "description")
    return "\n".join(strip.text for strip in card.strips(card.size.width or 120))


async def open_review(app: RevvApp, pilot):
    from revv.ui.review import ReviewScreen

    for _ in range(60):
        await pilot.pause(0.05)
        screen = app.screen
        if isinstance(screen, ReviewScreen) and screen.session.loaded:
            await pilot.pause(0.3)
            return screen
    raise AssertionError("never loaded")


async def test_a_long_description_is_summarized_and_d_toggles(summaries) -> None:
    app = RevvApp(long_demo(), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await open_review(app, pilot)
        assert summaries.asked == [LONG] and summaries.brief == [False]
        conversation = screen.conversation
        assert conversation.showing_summary
        text = description_text(screen)
        assert "Summary by Claude" in text and "Retries back off exponentially." in text
        assert "Paragraph 1:" not in text
        await pilot.press("D")
        await pilot.pause()
        assert not conversation.showing_summary
        text = description_text(screen)
        assert "Paragraph 1:" in text and "D shows Claude's summary" in text
        await pilot.press("1", "D")  # from the files tab too: back to the conversation
        await pilot.pause()
        assert screen.tab == "conversation" and conversation.showing_summary


async def test_short_descriptions_are_summarized_only_on_request(summaries, monkeypatch) -> None:
    backend = DemoBackend(latency=0)
    app = RevvApp(backend, target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await open_review(app, pilot)
        assert summaries.asked == []  # the demo's description fits on the screen
        await pilot.press("1", "D")  # asks for one anyway (and shows the conversation)
        await pilot.pause(0.3)
        assert summaries.asked == [backend._pr.body.strip()]
        assert summaries.brief == [True]  # it fits on a screen: about half its length
        assert screen.tab == "conversation" and screen.conversation.showing_summary
        await pilot.press("D")
        await pilot.pause()
        assert not screen.conversation.showing_summary and len(summaries.asked) == 1
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    app = RevvApp(long_demo(), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        await open_review(app, pilot)
        await pilot.press("D")
        await pilot.pause(0.2)
        assert len(summaries.asked) == 1  # no key: nothing is sent


async def test_summaries_are_cached_until_the_description_changes_enough(
    summaries, tmp_path
) -> None:
    cache = DiskCache(tmp_path / "cache")

    async def visit(body: str) -> None:
        app = RevvApp(long_demo(body), target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
        async with app.run_test(size=(140, 45)) as pilot:
            screen = await open_review(app, pilot)
            await pilot.pause(0.3)
            assert screen.conversation.showing_summary

    await visit(LONG)
    await visit(LONG)  # cached
    await visit(LONG + "\n\nOne more sentence at the end.")  # a small edit: still fits
    assert summaries.asked == [LONG]
    rewritten = LONG[: len(LONG) // 2] + "\n\nThe rest was rewritten entirely." * 40
    await visit(rewritten)  # more than 10% changed: summarized again
    assert summaries.asked == [LONG, rewritten]


async def test_a_new_prompt_summarizes_again(summaries, tmp_path, monkeypatch) -> None:
    cache = DiskCache(tmp_path / "cache")

    async def visit() -> None:
        app = RevvApp(long_demo(), target=DEMO_REF, repo=DEMO_REF.repo, cache=cache)
        async with app.run_test(size=(140, 45)) as pilot:
            await open_review(app, pilot)
            await pilot.pause(0.3)

    await visit()
    await visit()
    assert len(summaries.asked) == 1
    monkeypatch.setattr(summarizer, "VERSION", "a-new-prompt")
    await visit()
    assert len(summaries.asked) == 2


async def test_panc_always_reads_the_original_description(summaries, monkeypatch) -> None:
    from revv import panc
    from revv.models import AiCheck

    read: list[str] = []

    async def fake_check(text: str, program: str | None = None) -> AiCheck:
        read.append(text)
        return AiCheck(text=text, verdict="Human", fraction_human=1.0, checked_at=1.0)

    save_config(panc=True)
    monkeypatch.setattr(panc, "executable", lambda: "/usr/local/bin/panc")
    monkeypatch.setattr(panc, "check", fake_check)
    app = RevvApp(long_demo(), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await open_review(app, pilot)
        await pilot.pause(0.3)
        assert screen.conversation.showing_summary
        assert read and all(text == LONG for text in read)  # never the summary
        assert "panc on the original" in description_text(screen)
        await pilot.press("D")
        await pilot.pause()
        text = description_text(screen)
        assert "panc · " in text and "panc on the original" not in text
