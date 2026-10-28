"""Exceptions raised for input that cannot be converted."""


class ConversionError(ValueError):
    """The input cannot be converted (unsupported or malformed)."""


class UnsupportedFormatError(ConversionError):
    """The file is not a map format bsp2vmf can read."""


class MapParseError(ConversionError):
    """A ``.map`` file is malformed."""
