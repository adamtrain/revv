import pytest

from revv.models import PRRef, RepoRef
from revv.targets import parse_pr_ref, parse_remote_url, parse_repo

REPO = RepoRef("acme", "netkit")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("123", PRRef(REPO, 123)),
        ("#123", PRRef(REPO, 123)),
        (" 7 ", PRRef(REPO, 7)),
        ("other/thing#9", PRRef(RepoRef("other", "thing"), 9)),
        ("https://github.com/o/r/pull/55", PRRef(RepoRef("o", "r"), 55)),
        ("https://github.com/o/r/pull/55/files#diff-abc", PRRef(RepoRef("o", "r"), 55)),
        ("github.com/o/r/pull/5", PRRef(RepoRef("o", "r"), 5)),
        ("https://ghe.example.com/o/r/pull/3", PRRef(RepoRef("o", "r", "ghe.example.com"), 3)),
        ("fix the thing", None),
        ("owner/repo", None),
    ],
)
def test_parse_pr_ref(text, expected):
    assert parse_pr_ref(text, REPO) == expected


def test_parse_pr_ref_number_needs_a_repo():
    assert parse_pr_ref("12", None) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/netkit.git", RepoRef("acme", "netkit")),
        ("git@github.com:acme/netkit", RepoRef("acme", "netkit")),
        ("https://github.com/acme/netkit.git", RepoRef("acme", "netkit")),
        ("https://github.com/acme/netkit", RepoRef("acme", "netkit")),
        ("https://user@github.com/acme/netkit.git", RepoRef("acme", "netkit")),
        ("ssh://git@github.com/acme/netkit.git", RepoRef("acme", "netkit")),
        ("ssh://git@ssh.github.com:443/acme/netkit.git", RepoRef("acme", "netkit")),
        ("git@ghe.corp.com:team/app.git", RepoRef("team", "app", "ghe.corp.com")),
        ("/some/local/path", None),
    ],
)
def test_parse_remote_url(url, expected):
    assert parse_remote_url(url) == expected


def test_parse_repo():
    assert parse_repo("acme/netkit") == REPO
    assert parse_repo("https://github.com/acme/netkit") == REPO
    assert parse_repo("nonsense") is None
