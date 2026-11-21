"""The integration ships its own brand images (upstream f4332f1).

Home Assistant 2026.3+ reads a custom integration's icon and logo from a
brand/ folder next to manifest.json, ahead of the brands CDN, whose
custom_integrations folder is now legacy. Static files only: these tests check
the files are where Home Assistant looks and are real PNGs of the sizes the
brands repository requires.
"""

from pathlib import Path
import struct

import pytest

BRAND = Path("custom_components/qolsys_panel/brand")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# file name -> (width, height) in pixels, per home-assistant/brands rules
EXPECTED = {
    "icon.png": (256, 256),
    "dark_icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "dark_icon@2x.png": (512, 512),
    "logo.png": (892, 256),
    "dark_logo.png": (892, 256),
    "logo@2x.png": (1784, 512),
    "dark_logo@2x.png": (1784, 512),
}


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk, which must come first."""
    assert data[:8] == PNG_SIGNATURE, "not a PNG (bad signature)"
    length, chunk_type = struct.unpack(">I4s", data[8:16])
    assert chunk_type == b"IHDR" and length == 13, "first chunk is not IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_brand_folder_sits_next_to_the_manifest() -> None:
    """HA reads brand/ from the integration's own directory."""
    assert (BRAND.parent / "manifest.json").is_file()
    assert BRAND.is_dir()


@pytest.mark.parametrize(("name", "size"), sorted(EXPECTED.items()))
def test_brand_image_is_a_valid_png(name: str, size: tuple[int, int]) -> None:
    """Each image exists, is a PNG, ends with IEND, and has the required size."""
    path = BRAND / name
    assert path.is_file(), f"missing {path}"
    data = path.read_bytes()
    assert _png_size(data) == size
    assert data.endswith(b"IEND\xaeB\x60\x82"), "PNG is truncated (no IEND chunk)"


def test_brand_folder_holds_only_the_eight_images() -> None:
    """Static images only: nothing executable or unexpected rides along."""
    assert sorted(p.name for p in BRAND.iterdir()) == sorted(EXPECTED)
