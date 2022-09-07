"""defines basic data type for storing an individual sequence record"""
from collections import Counter
from collections.abc import MutableSequence
from math import isclose
from typing import Dict, Optional, Union

import numba
import numpy

from attrs import asdict, define, field, validators
from cogent3 import get_moltype
from cogent3.app.typing import SeqType
from numpy import array, log2, ndarray, ravel_multi_index, zeros


NumType = Union[float, int]
PosDictType = Dict[int, NumType]


def _gettype(name):
    import numpy

    if name[-1].isdigit():
        return getattr(numpy, name)
    else:
        return {"int": int, "float": float}[name]


@define(slots=True)
class sparse_vector(MutableSequence):
    data: dict
    size: int
    default: Optional[NumType] = field(init=False)
    dtype: type = float
    source: str = None
    name: str = None

    def __init__(
        self,
        *,
        size: int,
        data: PosDictType = None,
        dtype: type = float,
        source: str = None,
        name: str = None,
    ):
        """

        Parameters
        ----------
        size
            num_states**k
        data
            dict of {k-mer index: NumType}
        dtype
        """
        self.size = size
        self.dtype = dtype

        data = data or {}
        self.default = dtype(0)
        self.data = {i: n for i, n in data.items() if not isclose(n, 0)}
        self.source = source
        self.name = name

    def __setitem__(self, key: int, value: NumType):
        if isclose(value, 0):
            return
        try:
            key = key.item()
        except AttributeError:
            pass
        self.data[key] = self.dtype(value)

    def __getitem__(self, key: int) -> NumType:
        return self.data.get(key, self.default)

    def __delitem__(self, key: int):
        try:
            del self.data[key]
        except KeyError:
            pass

    def __len__(self) -> int:
        return self.size

    def __iter__(self) -> NumType:
        yield from (self[i] for i in range(len(self)))

    def _sub_vector(self, data, other) -> dict:
        assert self.size == len(other)
        for pos, num in other.data.items():
            data[pos] = self[pos] - num
        return data

    def _sub_scalar(self, data, scalar) -> dict:
        scalar = self.dtype(scalar)
        for pos, num in data.items():
            data[pos] -= scalar
        return data

    def __sub__(self, other):
        func = (
            self._sub_vector if isinstance(other, self.__class__) else self._sub_scalar
        )
        data = func({**self.data}, other)
        return self.__class__(data=data, size=self.size, dtype=self.dtype)

    def __isub__(self, other):
        func = (
            self._sub_vector if isinstance(other, self.__class__) else self._sub_scalar
        )
        self.data = func(self.data, other)
        return self

    def _add_vector(self, data, other) -> dict:
        assert self.size == len(other)
        for pos, num in other.data.items():
            data[pos] = self[pos] + num
        return data

    def _add_scalar(self, data, scalar) -> dict:
        scalar = self.dtype(scalar)
        for pos, num in data.items():
            data[pos] += scalar
        return data

    def __add__(self, other):
        # we are creating a new instance
        func = (
            self._add_vector if isinstance(other, self.__class__) else self._add_scalar
        )
        data = func({**self.data}, other)
        return self.__class__(data=data, size=self.size, dtype=self.dtype)

    def __iadd__(self, other):
        func = (
            self._add_vector if isinstance(other, self.__class__) else self._add_scalar
        )
        self.data = func(self.data, other)
        return self

    def _truediv_vector(self, data, other) -> dict:
        assert self.size == len(other)
        for pos, num in other.data.items():
            data[pos] = self[pos] / num
        return data

    def _truediv_scalar(self, data, scalar) -> dict:
        scalar = self.dtype(scalar)
        for pos, num in data.items():
            data[pos] = self[pos] / scalar
        return data

    def __truediv__(self, other):
        func = (
            self._truediv_vector
            if isinstance(other, self.__class__)
            else self._truediv_scalar
        )
        # we are creating a new instance
        data = func({**self.data}, other)
        return self.__class__(data=data, size=self.size, dtype=float)

    def __itruediv__(self, other):
        func = (
            self._truediv_vector
            if isinstance(other, self.__class__)
            else self._truediv_scalar
        )
        self.data = func(self.data, other)
        self.dtype = float
        return self

    def insert(self, index: int, value: NumType) -> None:
        self[index] = value

    def sum(self):
        return sum(self.data.values())

    @property
    def array(self) -> ndarray:
        arr = zeros(self.size, dtype=self.dtype)

        for index, val in self.data.items():
            arr[index] = val
        return arr

    def iter_nonzero(self) -> NumType:
        yield from (v for _, v in sorted(self.data.items()))

    @property
    def entropy(self):
        kfreqs = array(list(self.iter_nonzero()))
        return -(kfreqs * log2(kfreqs)).sum()

    def to_rich_dict(self):
        data = asdict(self)
        data["dtype"] = data["dtype"].__name__
        data.pop("default")
        # convert coords to str so orjson copes
        data["data"] = list(data["data"].items())
        return data

    @classmethod
    def from_dict(cls, data):
        data["data"] = dict(data["data"])
        data["dtype"] = _gettype(data["dtype"])
        return cls(**data)


@numba.jit
def seq2array(seq, arr, order):
    num_states = len(order)
    for i in range(len(seq)):
        char_index = -1
        c = seq[i]
        for j in range(num_states):
            if c == order[j]:
                char_index = j
                break

        arr[i] = char_index
    return arr


@numba.jit
def kmer_indices(seq, coeffs, result, k):
    skip_until = 0
    for i in range(k):
        if seq[i] < 0:
            skip_until = i + 1

    for i in range(len(result)):
        if seq[i + k - 1] < 0:
            skip_until = i + k
        if i < skip_until:
            index = -1
        else:
            kmer = seq[i : i + k]
            index = (kmer * coeffs).sum()
        result[i] = index
    return result


def seq_to_kmer_counts(seq: SeqType, moltype: "MolType", k: int) -> sparse_vector:
    """count k-mers

    Parameters
    ----------
    seq : Sequence
        cogent3 sequence
    moltype : MolType
        cogent3 molecular type
    k : int
        size of k-mers

    Returns
    -------
    ndarray
        float of raw counts. This has length (number of moltype states)**k

    Raises
    ------
    ValueError
        if the mapping from characters to integers is not sequential

    Notes
    -----
    The sequence is converted to indices using order of canonical characters
    defined by moltype. Each k-mer is then a k-dimension coordinate. We
    convert those into a 1D coordinate. Use ``numpy.unravel`` and the moltype
    to convert the indices back into a sequence.
    """
    # we make counts
    moltype = get_moltype(moltype)
    canonical = set(moltype.alphabet)
    num_states = len(canonical)
    chars2ints = moltype.alphabet.to_indices
    v = chars2ints(canonical)
    if not (0 <= min(v) < max(v) < num_states):
        raise ValueError(f"indices of canonical states {canonical} not sequential {v}")

    kwargs = dict(
        size=num_states ** k,
        dtype=int,
        source=getattr(seq, "source", None),
        name=seq.name,
    )

    # positions with non-canonical characters are assigned -1
    arr = numpy.zeros(len(seq), dtype=numpy.int8)
    can = "".join(moltype.alphabet)
    seq = seq2array(seq._seq.encode("utf8"), arr, can.encode("utf8"))

    # check for crazy big k
    dtype = numpy.int64
    if num_states ** k > 2 ** 64:
        raise NotImplementedError(f"{num_states}**{k} is too big for 64-bit integer")

    # k-mers with -1 are excluded
    coeffs = numpy.array(coord_conversion_coeffs(num_states, k), dtype=dtype)
    result = numpy.zeros(len(seq) - k + 1, dtype=dtype)
    result = kmer_indices(seq, coeffs, result, k)
    counts = Counter(v for v in result.tolist() if v >= 0)
    kwargs["data"] = counts
    return sparse_vector(**kwargs)


def coord_conversion_coeffs(num_states, k):
    """coefficients for multi-dimensional coordinate conversion into 1D index"""
    return [num_states ** (i - 1) for i in range(k, 0, -1)]


@numba.jit
def coord_to_index(coord, coeffs):
    """converts a multi-dimensional coordinate into a 1D index"""
    return (coord * coeffs).sum()


@numba.jit
def index_to_coord(index, coeffs):
    """converts a 1D index into a multi-dimensional coordinate"""
    ndim = len(coeffs)
    coord = numpy.zeros(ndim, dtype=numpy.int64)
    remainder = index
    for i in range(ndim):
        n, remainder = numpy.divmod(remainder, coeffs[i])
        coord[i] = n
    return coord


def _gt_zero(instance, attribute, value):
    if value <= 0:
        raise ValueError(f"must be > 0, not {value}")


@define(slots=True, order=True)
class SeqRecord:
    """representation of a single sequence as kmers"""

    k: int = field(validator=[_gt_zero, validators.instance_of(int)])
    name: str = field(init=False, validator=validators.instance_of(str))
    length: int = field(init=False, validator=_gt_zero)
    entropy: float = field(init=False, validator=validators.instance_of(float))
    kfreqs: sparse_vector = field(init=False)
    delta_jsd: float = field(
        init=False,
        validator=validators.instance_of(float),
        default=0.0,
        eq=True,
    )
    size: int = field(init=False)

    def __init__(self, k: int, seq: SeqType, moltype: Union[str, "MolType"]):
        """

        Parameters
        ----------
        k
            word size
        seq
            cogent3 Sequence instance
        moltype
            cogent3 MolType instance
        """
        self.__attrs_init__(k=k)
        self.name = seq.name
        self.length = len(seq)
        if self.k > self.length:
            raise ValueError(f"k={self.k} > length={self.length}")

        moltype = get_moltype(moltype)
        kcounts = seq_to_kmer_counts(seq, moltype, k)
        self.kfreqs = kcounts / kcounts.sum()
        kfreqs = array(list(self.kfreqs.iter_nonzero()))
        self.entropy = -(kfreqs * log2(kfreqs)).sum()
        self.size = self.kfreqs.size
