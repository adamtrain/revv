# revv

A fast, keyboard-driven terminal UI for reviewing GitHub pull requests, built with
[Textual](https://textual.textualize.io/).

- **A review inbox.** Run `revv` inside a repository to see every open pull request where
  your review was requested (directly or through one of your teams) or that is assigned to
  you. Tabs also list the ones you've reviewed and your own. Stacked pull requests are
  grouped in stack order, the oldest come first (`s` flips that), and `i` ignores a pull
  request (it moves to an "Ignored" tab). Anything else is one PR number or URL away:
  just type it.
- **A pleasant diff.** Every changed file in one scrollable view, syntax-highlighted, with
  word-level highlighting of what changed on a line. Switch between unified and
  side-by-side, expand hidden context, fold files, and jump by change, file or thread. The
  file tree lists folders first, and the current file stays pinned at the top as you scroll.
- **Comments where they belong.** Threads appear inline under their lines. You can comment
  on a line, a range, or a whole file, suggest changes (```` ```suggestion ````), reply,
  edit, delete, and resolve or unresolve.
- **Changes since your last review.** `L` narrows the diff to what changed since the
  commit you last reviewed, like GitHub's "changes since your last review".
- **The conversation.** Pull requests open here (`"open_tab": "files"` changes that): the description, every review thread at a glance, and the
  timeline. Post general comments, and resolve them the way GitHub does (hide them as
  "resolved").
- **Reviews the GitHub way.** Comments go into a pending review, just like "Start a review"
  on github.com, so nothing is lost if you quit. Submit with Comment, Approve or Request
  changes. When no review is pending you can also post a single comment right away.
- **Instant, and never stale for long.** Pull requests and the inbox open straight from
  an on-disk cache (clearly marked as such) and sync with GitHub right away. While you
  review, revv checks every 30 seconds whether the pull request changed and offers to
  refresh (`R`). File contents are cached by commit, so they're never fetched twice.
- **Viewed files.** Mark files as viewed (synced with GitHub's checkbox) and move on to the
  next unviewed one; the header shows how far along you are, weighted by changed lines.
  Test files and generated files are hidden by default (`T` / `X` show them).
- **Nicknames.** Press `@` to give people the names you'd rather see than their logins.

## Install

revv needs Python 3.12+ and uses the [GitHub CLI](https://cli.github.com/)'s login (or a
`GH_TOKEN` / `GITHUB_TOKEN` environment variable).

```sh
uv tool install git+https://github.com/adamtrain/revv
gh auth login   # if you haven't already
```

## Usage

```sh
revv                  # your review inbox for the repository you're in
revv 123              # review pull request #123 of this repository
revv owner/repo#123   # any pull request (a PR URL works too)
revv owner/repo       # the inbox of another repository
revv --all            # your inbox across all repositories
revv --demo           # try it with built-in sample data, no network needed
revv --no-cache       # don't read or write the cache in ~/.cache/revv
```

GitHub Enterprise works too: revv follows the host of your git remote or the PR URL.

## Keys

Press `?` in the app for the full list. The ones you'll use constantly:

| Key | In the diff |
| --- | --- |
| `j` `k` / `space` `ctrl+b` | move / page |
| `}` `{` | next / previous change |
| `]` `[` | next / previous file |
| `n` `N` | next / previous comment thread |
| `u` `U` | next / previous unresolved thread |
| `c` or `↵` | comment on the line (on a file header: comment on the file) |
| `V` then `c` | comment on a range of lines |
| `s` | suggest a change |
| `r` · `x` · `e` · `d` | reply · resolve/unresolve · edit · delete |
| `+` | react with an emoji (on comments, reviews and the description) |
| `v` | mark the file as viewed and go to the next unviewed one |
| `T` · `X` | show test files · generated files (they start hidden); again: hide them and mark them viewed |
| `\|` | unified / side-by-side |
| `L` | only the changes since your last review (again: everything) |
| `/` | search the changed lines |
| `f` | go to file |
| `t` · `<` `>` | hide the file tree · make it narrower / wider (remembered) |
| `S` · `A` | submit review · approve |
| `C` | comment on the pull request |
| `1` · `2` | files · conversation |
| `o` · `y` | open in browser · copy `path:line` |
| `R` | refresh (applies changes revv noticed on GitHub) |
| `@` | nicknames |
| `q` | back to the inbox (or quit) |

In the comment editor, `ctrl+s` adds the comment to your review (or saves), `ctrl+g` posts
it right away when no review is pending, `ctrl+t` inserts a suggestion block, `ctrl+o`
opens your `$EDITOR`, and `esc` cancels, keeping your draft for next time.

The command palette (`ctrl+p`) has all actions and lets you switch themes. The diff
colours follow the theme, and your choice is remembered.

Settings live in `~/.config/revv/config.json` and are all optional:

```json
{
  "hide_by_default": ["test", "generated"],
  "nicknames": {"octocat": "Octo"},
  "inbox_sort": "asc",
  "refresh_interval": 30,
  "sidebar_width": 34,
  "open_tab": "conversation",
  "panc": false
}
```

### AI-writing check with panc (optional)

If you have [panc](https://github.com/adamtrain/panc) (Pangram's AI detection in your
terminal) on your `PATH` and set `"panc": true`, revv runs it on each pull request's
description in the background. The verdict shows on the description card in the
conversation, with a chip in the header. Results are cached per pull request and only
re-checked when the description changes by at least 10%. It's off by default because
panc sends the description to Pangram (with your `PANGRAM_API_KEY`).

### What counts as a test or generated file

revv matches naming and folder conventions, not the word "test" (so `latest.py`,
`contest.ts` or `testimonials.tsx` are never matched):

- **Python:** `test_*.py`, `*_test.py`, `conftest.py`, Django's `tests.py`, and anything
  in a `tests/` or `test/` folder.
- **JavaScript / TypeScript:** `*.test.*`, `*.spec.*`, `*.e2e-spec.*`, `*.cy.*`,
  `__tests__/`, `__mocks__/`, Jest setup files, and `test/` / `tests/` folders.
- Snapshot folders (`__snapshots__/`, `*.snap`).
- **Generated:** lockfiles, minified files and source maps, protobuf/gRPC output,
  `__generated__/` folders, files with an `@generated` or `DO NOT EDIT` header, and
  anything your repository marks `linguist-generated` in `.gitattributes`.

## Development

```sh
uv sync
uv run revv --demo        # the offline demo
uv run pytest             # unit and end-to-end UI tests (offline)
uv run ruff check && uv run ty check src
```

The tests drive the whole app with simulated key presses against an in-memory backend,
so they need no network or GitHub account.

### Live checks against GitHub (opt-in)

`scripts/live_check.py` exercises every write path (comments, replies, resolving, viewed
files, conversation comments…) against the real GitHub API. It never runs as part of the
test suite. Run it yourself when you want it:

```sh
uv run scripts/live_check.py --yes
```

It uses your own credentials and your own account: on the first run it creates a
**private** repository named `<your login>/revv-sandbox` with a small pull request and one
permanent review comment. It needs that comment because submitted reviews can't be
deleted through the API. Later runs clean up everything they create. Delete the
repository whenever you like; the next run sets it up again. Without `--yes` it only
explains what it would do.
