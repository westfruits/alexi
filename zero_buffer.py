import os

import six
from six.moves import xrange

import cffi


_ffi = cffi.FFI()
_ffi.cdef("""
ssize_t read(int, void *, size_t);
ssize_t write(int, const void *, size_t);

int memcmp(const void *, const void *, size_t);
void *memchr(const void *, int, size_t);
void *Zero_memrchr(const void *, int, size_t);
void *memcpy(void *, const void *, size_t);
""")
_lib = _ffi.verify("""
#include <string.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <unistd.h>

#ifdef __GNU_SOURCE
#define Zero_memrchr memrchr
#else
void *Zero_memrchr(const void *s, int c, size_t n) {
    const unsigned char *cp;
    if (n != 0) {
        cp = (unsigned char *)s + n;
        do {
            if (*(--cp) == (unsigned char)c) {
                return (void *)cp;
            }
        } while (--n != 0);
    }
    return NULL;
}
#endif
""", extra_compile_args=["-D_GNU_SOURCE"])

BLOOM_WIDTH = _ffi.sizeof("long") * 8


class BufferFull(Exception):
    pass


class Buffer(object):
    def __init__(self, data, writepos):
        self._data = data
        self._writepos = writepos

    @classmethod
    def allocate(cls, size):
        return cls(_ffi.new("uint8_t[]", size), 0)

    def __repr__(self):
        return "Buffer(data=%r, capacity=%d, free=%d)" % (
            [self._data[i] for i in xrange(self.writepos)],
            self.capacity, self.free
        )

    @property
    def writepos(self):
        return self._writepos

    @property
    def capacity(self):
        return len(self._data)

    @property
    def free(self):
        return self.capacity - self.writepos

    def read_from(self, fd):
        if not self.free:
            raise BufferFull
        res = _lib.read(fd, self._data + self.writepos, self.free)
        if res == -1:
            raise OSError(_ffi.errno, os.strerror(_ffi.errno))
        elif res == 0:
            raise EOFError
        self._writepos += res
        return res

    def add_bytes(self, b):
        if not self.free:
            raise BufferFull
        bytes_written = min(len(b), self.free)
        for i in xrange(bytes_written):
            self._data[self.writepos] = six.indexbytes(b, i)
            self._writepos += 1
        return bytes_written

    def view(self, start=0, stop=None):
        if stop is None:
            stop = self.writepos
        if stop < start:
            raise ValueError("stop is less than start")
        if not (0 <= start <= self.writepos):
            raise ValueError(
                "The start is either negative or after the writepos"
            )
        if stop > self.writepos:
            raise ValueError("stop is after the writepos")
        return BufferView(self, self._data, start, stop)


class BufferView(object):
    def __init__(self, buf, data, start, stop):
        self._keepalive = buf
        self._data = data + start
        self._length = stop - start

    def __bytes__(self):
        return _ffi.buffer(self._data, self._length)[:]
    if six.PY2:
        __str__ = __bytes__

    def __repr__(self):
        return "BufferView(data=%r)" % (
            [self._data[i] for i in xrange(len(self))]
        )

    def __len__(self):
        return self._length

    def __eq__(self, other):
        if len(self) != len(other):
            return False
        if isinstance(other, BufferView):
            return _lib.memcmp(self._data, other._data, len(self)) == 0
        elif isinstance(other, bytes):
            for i in xrange(len(self)):
                if self[i] != six.indexbytes(other, i):
                    return False
            return True
        else:
            return NotImplemented

    def __ne__(self, other):
        return not (self == other)

    def __contains__(self, data):
        return self.find(data) != -1

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            start, stop, step = idx.indices(len(self))
            if step != 1:
                raise ValueError("Can't slice with non-1 step.")
            if start > stop:
                raise ValueError("Can't slice backwards.")
            return BufferView(self._keepalive, self._data, start, stop)
        else:
            if idx < 0:
                idx += len(self)
            if not (0 <= idx < len(self)):
                raise IndexError(idx)
            return self._data[idx]

    def __add__(self, other):
        if isinstance(other, BufferView):
            collator = BufferCollator()
            collator.append(self)
            collator.append(other)
            return collator.collapse()
        else:
            return NotImplemented

    def find(self, needle, start=0, stop=None):
        stop = stop or len(self)
        if start < 0:
            start = 0
        if stop > len(self):
            stop = len(self)
        if stop - start < 0:
            return -1

        if len(needle) == 0:
            return start
        elif len(needle) == 1:
            res = _lib.memchr(self._data + start, ord(needle), stop - start)
            if res == _ffi.NULL:
                return -1
            else:
                return _ffi.cast("uint8_t *", res) - self._data
        else:
            mask, skip = self._make_find_mask(needle)
            return self._multi_char_find(needle, start, stop, mask, skip)

    def index(self, needle, start=0, stop=None):
        idx = self.find(needle, start, stop)
        if idx == -1:
            raise ValueError("substring not found")
        return idx

    def rfind(self, needle, start=0, stop=None):
        stop = stop or len(self)
        if start < 0:
            start = 0
        if stop > len(self):
            stop = len(self)
        if stop - start < 0:
            return -1

        if len(needle) == 0:
            return start
        elif len(needle) == 1:
            res = _lib.Zero_memrchr(
                self._data + start, ord(needle), stop - start
            )
            if res == _ffi.NULL:
                return -1
            else:
                return _ffi.cast("uint8_t *", res) - self._data
        else:
            mask, skip = self._make_rfind_mask(needle)
            return self._multi_char_rfind(needle, start, stop, mask, skip)

    def rindex(self, needle, start=0, stop=None):
        idx = self.rfind(needle, start, stop)
        if idx == -1:
            raise ValueError("substring not found")
        return idx

    def split(self, by, maxsplit=-1):
        if len(by) == 0:
            raise ValueError("empty separator")
        elif len(by) == 1:
            return self._split_char(by, maxsplit)
        else:
            return self._split_multi_char(by, maxsplit)

    def _split_char(self, by, maxsplit):
        start = 0
        while maxsplit != 0:
            next = self.find(by, start)
            if next == -1:
                break
            yield self[start:next]
            start = next + 1
            maxsplit -= 1
        yield self[start:]

    def _split_multi_char(self, by, maxsplit):
        start = 0
        mask, skip = self._make_find_mask(by)
        while maxsplit != 0:
            next = self._multi_char_find(by, start, len(self), mask, skip)
            if next < 0:
                break
            yield self[start:next]
            start = next + len(by)
            maxsplit -= 1
        yield self[start:]

    def _bloom_add(self, mask, c):
        return mask | (1 << (c & (BLOOM_WIDTH - 1)))

    def _bloom(self, mask, c):
        return mask & (1 << (c & (BLOOM_WIDTH - 1)))

    def _make_find_mask(self, needle):
        mlast = len(needle) - 1
        mask = 0
        skip = mlast - 1
        for i in xrange(mlast):
            mask = self._bloom_add(mask, six.indexbytes(needle, i))
            if needle[i] == needle[mlast]:
                skip = mlast - i - 1
        mask = self._bloom_add(mask, six.indexbytes(needle, mlast))
        return mask, skip

    def _multi_char_find(self, needle, start, stop, mask, skip):
        i = start - 1
        w = (stop - start) - len(needle)
        while i + 1 <= start + w:
            i += 1
            if self._data[i + len(needle) - 1] == six.indexbytes(needle, -1):
                for j in xrange(len(needle) - 1):
                    if self._data[i + j] != six.indexbytes(needle, j):
                        break
                else:
                    return i
                if (
                    i + len(needle) < len(self) and
                    not self._bloom(mask, self._data[i + len(needle)])
                ):
                    i += len(needle)
                else:
                    i += skip
            else:
                if (
                    i + len(needle) < len(self) and
                    not self._bloom(mask, self._data[i + len(needle)])
                ):
                    i += len(needle)
        return -1

    def _make_rfind_mask(self, needle):
        mask = self._bloom_add(0, six.indexbytes(needle, 0))
        skip = len(needle) - 1
        for i in xrange(len(needle) - 1, 0, -1):
            mask = self._bloom_add(mask, six.indexbytes(needle, i))
            if needle[i] == needle[0]:
                skip = i - 1
        return mask, skip

    def _multi_char_rfind(self, needle, start, stop, mask, skip):
        i = start + (stop - start - len(needle)) + 1
        while i - 1 >= start:
            i -= 1
            if self[i] == six.indexbytes(needle, 0):
                for j in xrange(len(needle) - 1, 0, -1):
                    if self[i + j] != six.indexbytes(needle, j):
                        break
                else:
                    return i
                if i - 1 >= 0 and not self._bloom(mask, self[i - 1]):
                    i -= len(needle)
                else:
                    i -= skip
            else:
                if i - 1 >= 0 and not self._bloom(mask, self[i - 1]):
                    i -= len(needle)
        return -1

    def splitlines(self, keepends=False):
        i = 0
        j = 0
        while j < len(self):
            while (
                i < len(self) and
                self[i] != ord(b"\n") and self[i] != ord(b"\r")
            ):
                i += 1
            eol = i
            if i < len(self):
                if (
                    self[i] == ord(b"\r") and
                    i + 1 < len(self) and self[i + 1] == ord(b"\n")
                ):
                    i += 2
                else:
                    i += 1
                if keepends:
                    eol = i
            yield self[j:eol]
            j = i

    def isspace(self):
        if not self:
            return False
        for ch in self:
            if ch != 32 and not (9 <= ch <= 13):
                return False
        return True

    def isdigit(self):
        if not self:
            return False
        for ch in self:
            if not (ord("0") <= ch <= ord("9")):
                return False
        return True

    def isalpha(self):
        if not self:
            return False
        for ch in self:
            if not (65 <= ch <= 90 or 97 <= ch <= 122):
                return False
        return True

    def _strip_none(self, left, right):
        lpos = 0
        rpos = len(self)

        if left:
            while lpos < rpos and chr(self[lpos]).isspace():
                lpos += 1

        if right:
            while rpos > lpos and chr(self[rpos - 1]).isspace():
                rpos -= 1
        return self[lpos:rpos]

    def _strip_chars(self, chars, left, right):
        lpos = 0
        rpos = len(self)

        if left:
            while lpos < rpos and six.int2byte(self[lpos]) in chars:
                lpos += 1

        if right:
            while rpos > lpos and six.int2byte(self[rpos - 1]) in chars:
                rpos -= 1
        return self[lpos:rpos]

    def strip(self, chars=None):
        if chars is None:
            return self._strip_none(left=True, right=True)
        else:
            return self._strip_chars(chars, left=True, right=True)

    def lstrip(self, chars=None):
        if chars is None:
            return self._strip_none(left=True, right=False)
        else:
            return self._strip_chars(chars, left=True, right=False)

    def rstrip(self, chars=None):
        if chars is None:
            return self._strip_none(left=False, right=True)
        else:
            return self._strip_chars(chars, left=False, right=True)

    def write_to(self, fd):
        res = _lib.write(fd, self._data, self._length)
        if res == -1:
            raise OSError(_ffi.errno, os.strerror(_ffi.errno))
        return res


class BufferCollator(object):
    def __init__(self):
        self._views = []
        self._total_length = 0

    def __len__(self):
        return self._total_length

    def append(self, view):
        if self._views:
            last_view = self._views[-1]
            if (
                last_view._keepalive is view._keepalive and
                last_view._data + len(last_view) == view._data
            ):
                self._views[-1] = BufferView(
                    last_view._keepalive,
                    last_view._data,
                    0,
                    len(last_view) + len(view)
                )
            else:
                self._views.append(view)
        else:
            self._views.append(view)
        self._total_length += len(view)

    def collapse(self):
        if len(self._views) == 1:
            result = self._views[0]
        else:
            data = _ffi.new("uint8_t[]", self._total_length)
            pos = 0
            for view in self._views:
                _lib.memcpy(data + pos, view._data, len(view))
                pos += len(view)
            result = Buffer(data, self._total_length).view()
        del self._views[:]
        self._total_length = 0
        return result
