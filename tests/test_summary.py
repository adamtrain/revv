"""Claude's summaries of long pull request descriptions, with a fake `claude` executable
(nothing is sent anywhere)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

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

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
arguments = sys.argv[1:]
status = arguments[:2] == ["auth", "status"]
call = {{"arguments": arguments, "stdin": "" if status else sys.stdin.read(), "pid": os.getpid()}}
with open(os.environ["CLAUDE_CALLS"], "a") as calls:
    calls.write(json.dumps(call) + "\\n")
if status:
    signed_in = os.environ["CLAUDE_SIGNED_IN"] == "1"
    print(json.dumps({{"loggedIn": signed_in, "authMethod": "claude.ai" if signed_in else "none"}}))
    sys.exit(0 if signed_in else 1)
time.sleep(float(os.environ["CLAUDE_SLEEP"]))
sys.stdout.write(os.environ["CLAUDE_STDOUT"])
sys.stderr.write(os.environ["CLAUDE_STDERR"])
sys.exit(int(os.environ["CLAUDE_EXIT"]))
"""


class FakeClaude:
    """A `claude` that records how it was run, and answers what the test tells it to."""

    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.program = directory / "claude"
        self.program.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.program.chmod(0o755)
        self.log = directory / "claude-calls"
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("CLAUDE_CALLS", str(self.log))
        monkeypatch.setenv("CLAUDE_SLEEP", "0")
        monkeypatch.setattr(summarizer, "executable", lambda: str(self.program))
        self.sign_in(True)
        self.answer(SUMMARY)

    def sign_in(self, signed_in: bool) -> None:
        self.monkeypatch.setenv("CLAUDE_SIGNED_IN", "1" if signed_in else "0")

    def answer(self, result: str, *, code: int = 0, **fields: Any) -> None:
        """What `claude -p --output-format json` prints: its last message, the result."""
        reply = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "stop_reason": "end_turn",
            "modelUsage": {summarizer.MODEL: {"outputTokens": 11}},
            **fields,
        }
        self.print(json.dumps(reply), code=code)

    def print(self, stdout: str, stderr: str = "", *, code: int = 0) -> None:
        self.monkeypatch.setenv("CLAUDE_STDOUT", stdout)
        self.monkeypatch.setenv("CLAUDE_STDERR", stderr)
        self.monkeypatch.setenv("CLAUDE_EXIT", str(code))

    @property
    def calls(self) -> list[dict[str, Any]]:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [json.loads(line) for line in lines]

    @property
    def requests(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "-p" in call["arguments"]]


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    directory = tmp_path / "bin"
    directory.mkdir()
    return FakeClaude(directory, monkeypatch)


def option(request: dict[str, Any], name: str) -> str:
    arguments = request["arguments"]
    return arguments[arguments.index(name) + 1]


def test_only_with_claude_and_the_setting_on(fake_claude, monkeypatch) -> None:
    assert summarizer.available()
    save_config(summaries=False)
    assert not summarizer.available()
    save_config(summaries=True)
    monkeypatch.setattr(summarizer, "executable", lambda: None)
    assert not summarizer.available()


async def test_the_command(fake_claude, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # there's no key to set
    result = await summarizer.summarize("Back off exponentially", LONG)
    assert result.summary == SUMMARY and result.error is None and result.text == LONG
    assert result.model == "claude-sonnet-5-5"
    [request] = fake_claude.requests
    assert request["arguments"][0] == "-p"
    assert option(request, "--output-format") == "json"
    assert option(request, "--model") == "claude-sonnet-5-5"
    assert option(request, "--effort") == "low"
    assert option(request, "--tools") == ""  # the description can't talk claude into anything
    assert "--safe-mode" in request["arguments"]
    assert "--no-session-persistence" in request["arguments"]
    system = option(request, "--system-prompt")
    assert "What changed" in system and "How it was tested" in system
    assert "ASD-STE100 Simplified Technical English" in system
    assert "A descriptive sentence has a maximum of 25 words." in system
    assert "about 300 words and never more than 350" in system
    assert "<title>Back off exponentially</title>" in request["stdin"] and LONG in request["stdin"]


async def test_the_result_among_every_message_of_a_verbose_claude(fake_claude) -> None:
    result = {"type": "result", "is_error": False, "result": SUMMARY, "stop_reason": "end_turn"}
    fake_claude.print(json.dumps([{"type": "system"}, {"type": "assistant"}, result]))
    summary = await summarizer.summarize("t", LONG)
    assert summary.summary == SUMMARY and summary.model == summarizer.MODEL
    fake_claude.print(json.dumps([{"type": "system"}]))
    assert (await summarizer.summarize("t", LONG)).error == "claude returned something unexpected"


async def test_a_brief_summary_is_half_as_long_as_the_description(fake_claude) -> None:
    await summarizer.summarize("t", "A short description.", brief=True)
    [request] = fake_claude.requests
    system = option(request, "--system-prompt")
    assert "about half as long as the description" in system
    assert "300 words" not in system
    assert "Simplified Technical English" in system


async def test_refusals_and_errors(fake_claude, monkeypatch) -> None:
    fake_claude.answer("", stop_reason="refusal")
    refused = await summarizer.summarize("t", LONG)
    assert refused.refused and refused.error and not refused.summary
    fake_claude.answer(
        "API Error: Repeated 529 Overloaded errors", code=1, is_error=True, api_error_status=529
    )
    failed = await summarizer.summarize("t", LONG)
    assert failed.error == "API Error: Repeated 529 Overloaded errors"
    assert not failed.refused and not failed.summary
    fake_claude.answer("  ")
    empty = await summarizer.summarize("t", LONG)
    assert empty.error == "Claude returned an empty summary"
    fake_claude.print("", "warning: something\nerror: unknown option '--safe-mode'\n", code=1)
    old = await summarizer.summarize("t", LONG)
    assert old.error == "error: unknown option '--safe-mode'"
    fake_claude.print("Retries back off exponentially.")
    text = await summarizer.summarize("t", LONG)
    assert text.error == "claude returned something unexpected" and not text.summary
    monkeypatch.setattr(summarizer, "executable", lambda: None)
    missing = await summarizer.summarize("t", LONG)
    assert missing.error == "the claude CLI is not installed"


async def test_a_claude_that_hangs_is_stopped(fake_claude, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SLEEP", "60")
    monkeypatch.setattr(summarizer, "TIMEOUT", 0.5)
    result = await summarizer.summarize("t", LONG)
    assert result.error == "claude took too long to answer"
    [request] = fake_claude.requests
    for _ in range(100):
        try:
            os.kill(request["pid"], 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("claude is still running")


async def test_signed_in_is_what_claude_says(fake_claude, monkeypatch) -> None:
    fake_claude.sign_in(False)
    assert not await summarizer.signed_in()
    assert not await summarizer.signed_in()  # not asked about again right away...
    assert len(fake_claude.calls) == 1
    assert fake_claude.calls[0]["arguments"] == ["auth", "status", "--json"]
    fake_claude.sign_in(True)
    monkeypatch.setattr(summarizer, "SIGN_IN_RECHECK", 0.0)
    assert await summarizer.signed_in()  # ...but after a while, and by then you signed in
    fake_claude.sign_in(False)
    assert await summarizer.signed_in()  # a yes is remembered
    assert len(fake_claude.calls) == 2
    assert fake_claude.requests == []


async def test_signed_in_takes_a_claude_that_answers(fake_claude, monkeypatch) -> None:
    fake_claude.program.write_text(f"#!{sys.executable}\nprint('Unknown command: auth')\n")
    assert not await summarizer.signed_in()
    monkeypatch.setattr(summarizer, "_signed_in", None)
    monkeypatch.setattr(summarizer, "executable", lambda: None)
    assert not await summarizer.signed_in()


class Summaries:
    """A fake summarizer for the app: counts the descriptions it's asked about."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.brief: list[bool] = []
        self.signed_in = True

    async def __call__(self, title: str, body: str, *, brief: bool = False) -> DescriptionSummary:
        self.asked.append(body)
        self.brief.append(brief)
        return DescriptionSummary(text=body, summary=SUMMARY, model=summarizer.MODEL)

    async def is_signed_in(self) -> bool:
        return self.signed_in


@pytest.fixture
def summaries(monkeypatch) -> Summaries:
    fake = Summaries()
    monkeypatch.setattr(summarizer, "summarize", fake)
    monkeypatch.setattr(summarizer, "signed_in", fake.is_signed_in)
    monkeypatch.setattr(summarizer, "executable", lambda: "/usr/local/bin/claude")
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
    monkeypatch.setattr(summarizer, "executable", lambda: None)
    app = RevvApp(long_demo(), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        await open_review(app, pilot)
        await pilot.press("D")
        await pilot.pause(0.2)
        assert len(summaries.asked) == 1  # no claude: nothing is asked


async def test_signed_out_of_claude_nothing_happens_and_nothing_is_said(
    summaries, monkeypatch
) -> None:
    from revv.ui.review import ReviewScreen

    said: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ReviewScreen, "notify", lambda self, *message, **how: said.append(message))
    summaries.signed_in = False
    app = RevvApp(long_demo(), target=DEMO_REF, repo=DEMO_REF.repo)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await open_review(app, pilot)
        text = description_text(screen)
        assert "Paragraph 1:" in text and "Claude" not in text
        await pilot.press("1", "D")
        await pilot.pause(0.3)
        assert screen.tab == "files" and not screen.conversation.summarizing
        assert summaries.asked == [] and said == []
        summaries.signed_in = True  # signed in since: D works
        await pilot.press("D")
        await pilot.pause(0.3)
        assert summaries.asked == [LONG] and screen.conversation.showing_summary


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
