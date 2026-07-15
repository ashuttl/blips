"""blips — a live air-traffic scope in the terminal."""

try:
    from importlib.metadata import version
    __version__ = version("blips")
except Exception:
    __version__ = "dev"

USER_AGENT = f"blips/{__version__} (github.com/ashuttl/blips)"
