"""Optional AI-detection of pull request descriptions with panc (Pangram's CLI).

Enabled with `"panc": true` in the config, and only if `panc` is on your PATH (it needs
its own PANGRAM_API_KEY). The description is sent to Pangram by panc, so this is off by
default. Results are cached per pull request and only re-checked when the description
changes by at least 10%.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import shutil
import time

from revv.models import AiCheck, AiSegment

RECHECK_THRESHOLD = 0.10  # re-run once this much of the description has changed
TIMEOUT = 180


def executable() -> str | None:
    return shutil.which("panc")


def changed_enough(old: str, new: str) -> bool:
    """Whether at least 10% of the description changed since it was checked."""
    if old == new:
        return False
    if not old or not new:
        return True
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return 1 - matcher.ratio() >= RECHECK_THRESHOLD


def parse(text: str, data: dict) -> AiCheck:
    segments = []
    for window in data.get("windows") or []:
        excerpt = " ".join(str(window.get("text") or "").split())
        segments.append(
            AiSegment(
                label=str(window.get("label") or ""),
                ai_score=float(window.get("ai_assistance_score") or 0.0),
                confidence=str(window.get("confidence") or ""),
                excerpt=excerpt[:160],
                humanized=bool(window.get("is_humanized")),
            )
        )
    return AiCheck(
        text=text,
        verdict=str(data.get("prediction_short") or data.get("prediction") or ""),
        headline=str(data.get("headline") or ""),
        fraction_ai=float(data.get("fraction_ai") or 0.0),
        fraction_ai_assisted=float(data.get("fraction_ai_assisted") or 0.0),
        fraction_human=float(data.get("fraction_human") or 0.0),
        segments=segments,
        checked_at=time.time(),
    )


async def check(text: str, program: str | None = None) -> AiCheck:
    """Run `panc --json` on the text. Failures come back as an AiCheck with `error` set."""
    program = program or executable()
    if program is None:
        return AiCheck(text=text, error="panc is not installed", checked_at=time.time())
    try:
        process = await asyncio.create_subprocess_exec(
            program,
            "--json",
            "--file",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        out, err = await asyncio.wait_for(process.communicate(text.encode()), TIMEOUT)
    except (OSError, TimeoutError) as error:
        return AiCheck(text=text, error=f"couldn't run panc: {error}", checked_at=time.time())
    if process.returncode != 0:
        lines = [ln.strip() for ln in err.decode(errors="replace").splitlines() if ln.strip()]
        message = lines[-1] if lines else f"panc exited with {process.returncode}"
        return AiCheck(text=text, error=message, checked_at=time.time())
    try:
        data = json.loads(out.decode())
    except ValueError:
        return AiCheck(
            text=text, error="panc returned something unexpected", checked_at=time.time()
        )
    return parse(text, data)
