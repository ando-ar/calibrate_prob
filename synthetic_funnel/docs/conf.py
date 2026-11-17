"""Sphinx settings for the synthetic-funnel user guide."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sphinx.application import Sphinx

project = "synthetic-funnel"
language = "ja"
extensions = ["myst_parser", "sphinx_rtd_theme"]
html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_css_files = ["layout.css"]
html_show_copyright = False
html_copy_source = False
html_show_sourcelink = False

_REPOSITORY_FILE_LINK = re.compile(r"\[([^\]\n]+)\]\((\.\./(?:runs|src)/[^)#\n]+)\)")


def _link_repository_files(_app: "Sphinx", _docname: str, source: list[str]) -> None:
    """Copy linked examples and source files into the built documentation."""
    source[0] = _REPOSITORY_FILE_LINK.sub(
        lambda match: f"{{download}}`{match[1].strip('`')} <{match[2]}>`", source[0]
    )


def setup(app: "Sphinx") -> None:
    app.connect("source-read", _link_repository_files)
