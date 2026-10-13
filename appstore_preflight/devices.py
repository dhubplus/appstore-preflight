"""Screenshot pixel sizes and the App Store Connect display types they map to.

This table is knowledge about Apple's store, not about any particular app, and
it goes stale when Apple ships a new screen size. So an unrecognised size is
reported as unrecognised -- naming the size, and naming the configuration key
that teaches the tool about it -- rather than as a failure. A resized desktop
capture and a device size this table has not heard of look identical from here,
and only the person running it knows which they have.
"""

from __future__ import annotations

from .reading import Unreadable

#: (width, height) -> display type, portrait. Landscape is derived.
PORTRAIT_SIZES = {
    # iPhone
    (1320, 2868): "APP_IPHONE_67",          # 6.9"
    (1290, 2796): "APP_IPHONE_67",          # 6.7"
    (1284, 2778): "APP_IPHONE_67",          # 6.7" (older)
    (1242, 2688): "APP_IPHONE_65",          # 6.5"
    (1206, 2622): "APP_IPHONE_61",          # 6.3"
    (1179, 2556): "APP_IPHONE_61",          # 6.1"
    (1170, 2532): "APP_IPHONE_61",
    (1125, 2436): "APP_IPHONE_58",          # 5.8"
    (1242, 2208): "APP_IPHONE_55",          # 5.5"
    (750, 1334): "APP_IPHONE_47",           # 4.7"
    (640, 1136): "APP_IPHONE_40",           # 4.0"
    # iPad
    (2064, 2752): "APP_IPAD_PRO_3GEN_129",  # 13"
    (2048, 2732): "APP_IPAD_PRO_3GEN_129",  # 12.9"
    (1668, 2420): "APP_IPAD_PRO_3GEN_11",   # 11"
    (1668, 2388): "APP_IPAD_PRO_3GEN_11",
    (1640, 2360): "APP_IPAD_PRO_3GEN_11",
    (1668, 2224): "APP_IPAD_105",
    (1536, 2048): "APP_IPAD_97",
}


def display_types(extra: dict = None) -> dict:
    """{(width, height): display type} in both orientations."""
    table = {}
    for (width, height), display_type in PORTRAIT_SIZES.items():
        table[(width, height)] = display_type
        table[(height, width)] = display_type
    for key, value in (extra or {}).items():
        try:
            width, height = (int(part) for part in str(key).lower().split("x"))
        except ValueError:
            continue
        table[(width, height)] = value
        table[(height, width)] = value
    return table


def png_size(path):
    """(width, height), ``None`` when it is not a PNG, or an ``Unreadable``.

    Read from the header rather than with an image library, so the tool has no
    dependency for this and cannot be fooled by a file extension.

    The three-way answer matters: returning ``None`` for an unreadable file
    made the caller report "is named .png and is not a PNG", which is a
    confident and wrong statement about an image nobody opened.
    """
    try:
        head = path.read_bytes()[:26]
    except OSError as exc:
        return Unreadable(str(path), f"screenshot unreadable ({exc})")
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (
        int.from_bytes(head[16:20], "big"),
        int.from_bytes(head[20:24], "big"),
    )
