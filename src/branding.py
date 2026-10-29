from pathlib import Path

from PIL import Image
import streamlit as st

from .config import PROJECT_ROOT

DEFAULT_ICON = "🐦"


@st.cache_resource(show_spinner=False)
def _load_image(path, version):
    try:
        with Image.open(path) as image:
            image.load()
            return image.copy()
    except Exception:
        return None


def load_logo(theme="auto", assets_dir=None):
    directory = Path(assets_dir) if assets_dir is not None else PROJECT_ROOT / "assets"
    names = ("raven_light.png", "raven.png") if theme == "light" else ("raven.png",)
    for name in names:
        path = directory / name
        try:
            metadata = path.stat()
        except OSError:
            continue
        image = _load_image(str(path), (metadata.st_mtime_ns, metadata.st_size))
        if image is not None:
            return image
    return None
