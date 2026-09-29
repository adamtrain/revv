"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from revv import __version__
from revv.models import PRRef, RepoRef
from revv.targets import detect_repo, parse_pr_ref, parse_repo

DESCRIPTION = """\
Review GitHub pull requests in your terminal.

  revv                 your review inbox for the current repository
  revv 123             review pull request #123 of the current repository
  revv owner/repo#123  review a pull request anywhere (a PR URL works too)
  revv owner/repo      the review inbox of another repository
  revv --all           your review inbox across all repositories
  revv --demo          try it out with built-in demo data (no network)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revv",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", nargs="?", help="PR number, owner/repo#N, PR URL, or owner/repo")
    parser.add_argument("-R", "--repo", help="repository as owner/repo (default: from git remote)")
    parser.add_argument("-a", "--all", action="store_true", help="inbox across all repositories")
    parser.add_argument("--demo", action="store_true", help="use built-in demo data (offline)")
    parser.add_argument("--version", action="version", version=f"revv {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.demo:
        from revv.demo import DEMO_REF, DemoBackend

        backend = DemoBackend()
        repo: RepoRef | None = DEMO_REF.repo
        target: PRRef | None = parse_pr_ref(args.target, repo) if args.target else None
    else:
        from revv.github.backend import GitHubBackend
        from revv.github.client import AuthError, GitHubClient, find_token

        repo = parse_repo(args.repo) if args.repo else detect_repo()
        target = None
        if args.target:
            target = parse_pr_ref(args.target, repo)
            if target is None:
                other = parse_repo(args.target, repo.host if repo else "github.com")
                if other is None:
                    if repo is None and args.target.lstrip("#").isdigit():
                        print(
                            "revv: not inside a GitHub repository; use owner/repo#N or --repo",
                            file=sys.stderr,
                        )
                    else:
                        print(f"revv: don't know what {args.target!r} refers to", file=sys.stderr)
                    return 2
                repo = other
        host = target.repo.host if target else repo.host if repo else "github.com"
        try:
            token = find_token(host)
        except AuthError as error:
            print(f"revv: {error}", file=sys.stderr)
            return 1
        backend = GitHubBackend(GitHubClient(token, host))

    from revv.ui.app import RevvApp

    app = RevvApp(backend, target=target, repo=repo, all_repos=args.all)
    note = app.run()
    if note:
        print(note)
    return app.return_code or 0
