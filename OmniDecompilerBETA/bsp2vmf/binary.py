"""Helpers for reading binary BSP data."""

from __future__ import annotations

import io
import lzma
import struct
from typing import List, Tuple


class ByteWindow(io.RawIOBase):
    """Read-only file object over ``buffer[start:start + size]`` without copying it.

    Lets ``zipfile`` read an archive stored inside a memory-mapped BSP.
    """

    def __init__(self, buffer, start: int, size: int):
        super().__init__()
        self._buffer = buffer
        self._start = start
        self._size = size
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        self._position = max(0, min(self._size, base + offset))
        return self._position

    def readinto(self, target) -> int:
        count = max(0, min(len(target), self._size - self._position))
        begin = self._start + self._position
        target[:count] = self._buffer[begin:begin + count]
        self._position += count
        return count


def unpack_array(fmt: str, data: bytes) -> List[Tuple]:
    """Unpack as many complete ``fmt`` records as ``data`` holds."""
    size = struct.calcsize(fmt)
    count = len(data) // size
    return list(struct.iter_unpack(fmt, data[:count * size]))


def unpack_ints(data: bytes) -> List[int]:
    return [x[0] for x in unpack_array("<i", data)]


def c_string(data: bytes) -> str:
    """Decode a NUL-terminated Latin-1 string."""
    end = data.find(b"\0")
    if end >= 0:
        data = data[:end]
    return data.decode("latin-1")


def lzma_decompress(data: bytes) -> bytes:
    """Unwrap Valve's LZMA container; returns ``data`` unchanged if not compressed.

    Valve header: "LZMA", uint32 actual size, uint32 compressed size, 5 property
    bytes. Python's lzma module reads the equivalent "alone" format.
    """
    if len(data) < 17 or data[:4] != b"LZMA":
        return data
    actual_size, compressed_size = struct.unpack_from("<II", data, 4)
    properties = data[12:17]
    alone = properties + struct.pack("<Q", actual_size) + data[17:17 + compressed_size]
    return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(alone)[:actual_size]
