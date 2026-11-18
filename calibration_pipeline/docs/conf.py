"""Build the user guide and API reference from the installed editable package."""

from importlib.metadata import version as package_version

project = "calibration-pipeline"
release = package_version(project)
language = "ja"
extensions = [
    "myst_parser",
    "sphinx.ext.apidoc",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]
html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_css_files = ["layout.css"]
html_show_copyright = False
html_copy_source = False
html_show_sourcelink = False
exclude_patterns = ["_build", "html"]

apidoc_modules = [
    {
        "path": "../src/calibration_pipeline",
        "destination": "_generated",
        "separate_modules": True,
        "module_first": True,
    },
]
autodoc_member_order = "bysource"
autodoc_typehints = "signature"

