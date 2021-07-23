import functools
import os

import six
from six.moves import xrange, zip

import cffi


ffi = cffi.FFI()
ffi.cdef("""
ssize_t read(int, void *, size_t);

int memcmp(const void *, const void *, size_t);
void *memchr(const void *, int, size_t);
""")
lib = ffi.verify("""
#include <sys/types.h>
#include <sys/uio.h>
#include <unistd.h>
""")

BLOOM_WIDTH = ffi.sizeof("long") * 8


class BufferFull(Exception):
    pass


class BufferPool(object):
    def __init__(self, capacity, buffer_size):
        self.capacity = capacity
        self.buffer_size = buffer_size
        self._freelist = [self._create_buffer() for _ in xrange(capacity)]
        self._num_free = capacity

    def _create_buffer(self):
        return Buffer(self, self.buffer_size)

    def buffer(self):
        if self._num_free:
            self._num_free -= 1
            buf = self._freelist[self._num_free]
            self._freelist[self._num_free] = None
            return buf
        else:
            return self._create_buffer()

    def return_buffer(self, buffer):
        if self._num_free != self.capacity:
            self._freelist[self._num_free] = buffer
            self._num_free += 1


class Buffer(object):
    def __init__(self, pool, capacity):
        self.pool = pool
        self._data = ffi.new("uint8_t[]", capacity)
        self._writepos = 0

    def __repr__(self):
        return "Buffer(data=%r, capacity=%d, free=%d)" % (
            [self._data[i] for i in xrange(self.writepos)],
            self.capacity, self.free
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.release()

    @property
    def writepos(self):
        return self._writepos

    @property
    def capacity(self):
        return len(self._data)

    @property
    def free(self):
        return len(self._data) - self.writepos

    def release(self):
        self._writepos = 0
        self.pool.return_buffer(self)

    def read_from(self, fd):
        if not self.free:
            raise BufferFull
        res = lib.read(fd, self._data + self.writepos, len(self._data) - self.writepos)
        if res == -1:
            raise OSError(ffi.errno, os.strerror(ffi.errno))
        elif res == 0:
            raise EOFError
        self._writepos += res
        return res

    def add_bytes(self, b):
        if not self.free:
            raise BufferFull
        bytes_written = min(len(b), self.free)
        for i in xrange(bytes_written):
            self._data[self.writepos] = ord(b[i])
            self._writepos += 1
        return bytes_written

    def view(self, start=0, stop=None):
        if stop is None:
            stop = self.writepos
        if stop < start or not (0 <= start <= self.writepos) or stop > self.writepos:
            raise ValueError
        return BufferView(self, self._data, start, stop)


class _BaseBufferView(object):
    def _strip_none(self, left, right):
        lpos = iter(self)
        rpos = reversed(self)

        if left:
            while lpos < rpos:
                try:
                    ch = next(lpos)
                except StopIteration:
                    break
                if not chr(ch).isspace():
                    lpos._prev()
                    break

        if right:
            while rpos > lpos:
                try:
                    ch = next(rpos)
                except StopIteration:
                    break
                if not chr(ch).isspace():
                    rpos._prev()
                    break
        return self._slice_from_iterators(lpos, rpos)

    def _strip_chars(self, chars, left, right):
        lpos = iter(self)
        rpos = reversed(self)

        if left:
            while lpos < rpos:
                ch = next(lpos)
                if chr(ch) not in chars:
                    lpos._prev()
                    break

        if right:
            while rpos > lpos:
                try:
                    ch = next(rpos)
                except StopIteration:
                    break
                if chr(ch) not in chars:
                    rpos._prev()
                    break
        return self._slice_from_iterators(lpos, rpos)

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



class BufferView(_BaseBufferView):
    def __init__(self, buf, data, start, stop):
        self._keepalive = buf
        self._data = data + start
        self._length = stop - start

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
            return lib.memcmp(self._data, other._data, len(self)) == 0
        elif isinstance(other, bytes):
            for i in xrange(len(self)):
                if self[i] != ord(other[i]):
                    return False
            return True
        else:
            return NotImplemented

    def __ne__(self, other):
        return not (self == other)

    def __iter__(self):
        return _BufferViewIterator(self)

    def __reversed__(self):
        return _ReversedBufferViewIterator(self)

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
            if self._keepalive is other._keepalive and self._data + len(self) == other._data:
                return BufferView(self._keepalive, self._data, 0, len(self) + len(other))
            else:
                return BufferGroup([self, other])
        elif isinstance(other, BufferGroup):
            raise NotImplementedError
        else:
            return NotImplemented

    def _slice_from_iterators(self, lpos, rpos):
        return self[lpos._idx:rpos._idx]

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
            res = lib.memchr(self._data + start, ord(needle), stop - start)
            if res == ffi.NULL:
                return -1
            else:
                return ffi.cast("uint8_t *", res) - self._data
        else:
            mask, skip = self._make_find_mask(needle)
            return self._multi_char_find(needle, start, stop, mask, skip)

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
            mask = self._bloom_add(mask, ord(needle[i]))
            if needle[i] == needle[mlast]:
                skip = mlast - i - 1
        mask = self._bloom_add(mask, ord(needle[mlast]))
        return mask, skip

    def _multi_char_find(self, needle, start, stop, mask, skip):
        i = start - 1
        w = (stop - start) - len(needle)
        while i + 1 <= start + w:
            i += 1
            if self._data[i + len(needle) - 1] == ord(needle[-1]):
                for j in xrange(len(needle) - 1):
                    if self._data[i + j] != ord(needle[j]):
                        break
                else:
                    return i
                if i + len(needle) < len(self) and not self._bloom(mask, self._data[i + len(needle)]):
                    i += len(needle)
                else:
                    i += skip
            else:
                if i + len(needle) < len(self) and not self._bloom(mask, self._data[i + len(needle)]):
                    i += len(needle)
        return -1

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


class BufferGroup(_BaseBufferView):
    def __init__(self, views):
        self.views = views
        self._length = sum(len(view) for view in views)

    def __repr__(self):
        return "BufferGroup(%r)" % (self.views)

    def __len__(self):
        return self._length

    def __eq__(self, other):
        if isinstance(other, (BufferGroup, bytes)):
            if len(self) != len(other):
                return False
            for ch1, ch2 in zip(self, other):
                if isinstance(ch2, bytes):
                    ch2 = ord(ch2)
                if ch1 != ch2:
                    return False
            return True
        else:
            return NotImplemented

    def __ne__(self, other):
        return not (self == other)

    def __iter__(self):
        return _BufferGroupIterator(self)

    def __reversed__(self):
        return _ReversedBufferGroupIterator(self)

    def _find_positions_for_index(self, idx):
        for view_idx, view in enumerate(self.views):
            if idx < len(view):
                return view_idx, idx
            idx -= len(view)

        raise IndexError

    def _slice_from_iterators(self, lpos, rpos):
        start = [self.views[lpos._view_idx][lpos._buf_idx:]]
        middle = self.views[lpos._view_idx + 1:rpos._view_idx]
        stop = [self.views[rpos._view_idx][:rpos._buf_idx + 1]]
        return BufferGroup(start + middle + stop)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            start, stop, step = idx.indices(len(self))
            if step != 1:
                raise ValueError("Non-1 step is not supported.")
            if stop < start:
                raise ValueError("Reverse slice is not supported.")
            start_view_idx, start_idx = self._find_positions_for_index(start)
            if stop == len(self):
                return BufferGroup(
                    [self.views[start_view_idx][start_idx:]] +
                    self.views[start_view_idx + 1:]
                )
            else:
                stop_view_idx, stop_idx = self._find_positions_for_index(stop)
                if start_view_idx == stop_view_idx:
                    return self.views[start_view_idx][start_idx:stop_idx]
                return BufferGroup(
                    [self.views[start_view_idx][start_idx:]] +
                    self.views[start_view_idx + 1:stop_view_idx - 1] +
                    [self.views[stop_view_idx][:stop_idx]]
                )
        else:
            if idx < 0:
                idx += len(self)
            if not (0 <= idx < len(self)):
                raise IndexError(idx)
            view_idx, pos = self._find_positions_for_index(idx)
            return self.views[view_idx][pos]

    def __add__(self, other):
        if isinstance(other, BufferGroup):
            return BufferGroup(self.views + other.views)
        elif isinstance(other, BufferView):
            raise NotImplementedError
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
            overall_pos = 0
            for view in self.views:
                if start < len(view):
                    view_start = start
                else:
                    view_start = 0

                if stop < overall_pos + len(view):
                    view_stop = stop - overall_pos
                else:
                    view_stop = None

                pos = view.find(needle, view_start, view_stop)
                if pos != -1:
                    return overall_pos + pos
                overall_pos += len(view)
            return -1
        else:
            raise NotImplementedError

    def isspace(self):
        if not len(self):
            return False

        for view in self.views:
            if not view.isspace():
                return False

        return True

    def isalpha(self):
        if not len(self):
            return False

        for view in self.views:
            if not view.isalpha():
                return False

        return True

    def isdigit(self):
        if not len(self):
            return False

        for view in self.views:
            if not view.isdigit():
                return False

        return True


@functools.total_ordering
class _BufferViewIterator(object):
    def __init__(self, view):
        self._view = view
        self._idx = 0

    def __next__(self):
        try:
            ch = self._view[self._idx]
        except IndexError:
            raise StopIteration
        else:
            self._idx += 1
            return ch

    if not six.PY3:
        next = __next__

    def _prev(self):
        self._idx -= 1

    def __eq__(self, other):
        return self._view is other._view and self._idx == other._idx

    def __lt__(self, other):
        return self._view is other._view and self._idx < other._idx

@functools.total_ordering
class _ReversedBufferViewIterator(object):
    def __init__(self, view):
        self._view = view
        self._idx = len(self._view)

    def __next__(self):
        self._idx -= 1
        try:
            return self._view[self._idx]
        except IndexError:
            raise StopIteration

    if not six.PY3:
        next = __next__

    def _prev(self):
        self._idx += 1

    def __eq__(self, other):
        return self._view is other._view and self._idx == other._idx

    def __lt__(self, other):
        return self._view is other._view and self._idx < other._idx


@functools.total_ordering
class _BufferGroupIterator(object):
    def __init__(self, buffer_group):
        self._buffer_group = buffer_group
        self._view_idx = 0
        self._buf_idx = 0

    def __iter__(self):
        return self

    def __next__(self):
        try:
            buf = self._buffer_group.views[self._view_idx]
        except IndexError:
            raise StopIteration
        try:
            ch = buf[self._buf_idx]
        except IndexError:
            self._view_idx += 1
            self._buf_idx = 0
            return next(self)
        else:
            self._buf_idx += 1
            return ch

    if not six.PY3:
        next = __next__

    def _prev(self):
        if self._buf_idx == 0:
            self._view_idx -= 1
            self._buf_idx = len(self.buffer_group.views[self._view_idx]) - 1
        else:
            self._buf_idx -= 1


    def __eq__(self, other):
        return (
            self._buffer_group is other._buffer_group and
            self._view_idx == other._view_idx and
            self._buf_idx == other._buf_idx
        )

    def __lt__(self, other):
        return (
            self._buffer_group is other._buffer_group and
            self._view_idx < other._view_idx or (
                self._view_idx == other._view_idx and
                self._buf_idx < other._buf_idx
            )
        )


@functools.total_ordering
class _ReversedBufferGroupIterator(object):
    def __init__(self, buffer_group):
        self._buffer_group = buffer_group
        self._view_idx = len(buffer_group.views) - 1
        self._buf_idx = len(self._buffer_group.views[-1]) - 1

    def __next__(self):
        if self._view_idx < 0:
            raise StopIteration
        buf = self._buffer_group.views[self._view_idx]
        if self._buf_idx < 0:
            self._view_idx -= 1
            self._buf_idx = len(self._buffer_group.views[self._view_idx]) - 1
            return next(self)
        ch = buf[self._buf_idx]
        self._buf_idx -= 1
        return ch

    if not six.PY3:
        next = __next__

    def _prev(self):
        if self._buf_idx == len(self._buffer_group.views[self._view_idx]) - 1:
            self._buf_idx = 0
            self._view_idx += 1
        else:
            self._buf_idx += 1

    def __eq__(self, other):
        return (
            self._buffer_group is other._buffer_group and
            self._view_idx == other._view_idx and
            self._buf_idx == other._buf_idx
        )

    def __lt__(self, other):
        return (
            self._buffer_group is other._buffer_group and
            self._view_idx < other._view_idx or (
                self._view_idx == other._view_idx and
                self._buf_idx < other._buf_idx
            )
        )
