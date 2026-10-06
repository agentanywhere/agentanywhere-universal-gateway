"""Allow ``python -m app`` to launch the CLI."""
from app.cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
