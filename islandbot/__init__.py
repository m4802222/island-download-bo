"""Island Download Bot application package."""

from pathlib import Path

__all__ = ["__version__"]

__version__ = (
    Path(__file__)
    .resolve()
    .parent.parent.joinpath("VERSION")
    .read_text(encoding="utf-8")
    .strip()
)
