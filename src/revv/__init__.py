"""revv: review GitHub pull requests from the terminal."""

__version__ = "0.1.0"


def main() -> int:
    from revv.cli import main as cli_main

    return cli_main()
