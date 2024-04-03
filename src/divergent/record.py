"""defines basic data type for storing an individual sequence record"""
from dataclasses import dataclass
from functools import singledispatch
from math import fabs, isclose
from typing import Dict, Optional, Union

import numba

from attrs import asdict, define, field, validators
from cogent3 import get_moltype
from cogent3.app import composable
from cogent3.app import typing as c3_types
from cogent3.core.alphabet import get_array_type
from numpy import array
from numpy import divmod as np_divmod
from numpy import errstate, log2, nan_to_num, ndarray, uint8, uint64, zeros

from divergent import util as dv_utils


NumType = Union[float, int]
PosDictType = Dict[int, NumType]


@singledispatch
def _gettype(name) -> type:
    raise NotImplementedError


@_gettype.register
def _(name: str) -> type:
    import numpy

    if name[-1].isdigit():
        return getattr(numpy, name)
    else:
        return {"int": int, "float": float}[name]


@_gettype.register
def _(name: type) -> type:
    return name


@singledispatch
def _make_data(data, size: int | None = None, dtype: type = int) -> ndarray:
    raise NotImplementedError


@_make_data.register
def _(data: ndarray, size: int | None = None, dtype: type = int) -> ndarray:
    return data


@_make_data.register
def _(data: None, size: int | None = None, dtype: type = int) -> ndarray:
    return zeros(size, dtype=dtype)


@_make_data.register
def _(data: dict, size: int | None = None, dtype: type = int) -> ndarray:
    result = zeros(size, dtype=dtype)
    for i, n in data.items():
        if isclose(n, 0):
            continue
        result[i] = n
    return result


@define(slots=True)
class vector:
    data: ndarray
    vector_length: int
    default: Optional[NumType] = field(init=False)
    dtype: type = float
    source: str = None
    name: str = None

    def __init__(
        self,
        *,
        vector_length: int,
        data: ndarray = None,
        dtype: type = float,
        source: str = None,
        name: str = None,
    ):
        """

        Parameters
        ----------
        vector_length
            num_states**k
        data
            dict of {k-mer index: NumType}
        dtype
        """
        self.vector_length = vector_length
        dtype = _gettype(dtype)
        self.dtype = dtype

        data = _make_data(data, size=vector_length, dtype=dtype)
        self.default = dtype(0)
        self.data = data
        self.source = source
        self.name = name

    def __setitem__(self, key: int, value: NumType):
        self.data[key] = value

    def __getitem__(self, key: int) -> NumType:
        return self.data[key]

    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self) -> NumType:
        yield from self.data

    def __getstate__(self):
        return asdict(self)

    def __setstate__(self, data):
        for k, v in data.items():
            setattr(self, k, v)
        return self

    def __sub__(self, other):
        data = self.data - other
        return self.__class__(
            data=data, vector_length=self.vector_length, dtype=self.dtype
        )

    def __isub__(self, other):
        self.data -= other
        return self

    def __add__(self, other):
        # we are creating a new instance
        data = self.data + other
        return self.__class__(
            data=data, vector_length=self.vector_length, dtype=self.dtype
        )

    def __iadd__(self, other):
        self.data += other
        return self

    def __truediv__(self, other):
        # we are creating a new instance
        with errstate(divide="ignore", invalid="ignore"):
            data = nan_to_num(self.data / other, nan=0.0, copy=False)
        return self.__class__(data=data, vector_length=self.vector_length, dtype=float)

    def __itruediv__(self, other):
        with errstate(divide="ignore", invalid="ignore"):
            data = nan_to_num(self.data / other, nan=0.0, copy=False)
        self.dtype = float
        self.data = data
        return self

    def sum(self):
        return self.data.sum()

    def iter_nonzero(self) -> NumType:
        yield from (v for v in self.data if v)

    @property
    def entropy(self):
        non_zero = self.data[self.data > 0]
        if self.dtype == float:
            kfreqs = non_zero
        else:
            kfreqs = non_zero / non_zero.sum()

        # taking absolute value due to precision issues
        return fabs(-(kfreqs * log2(kfreqs)).sum())

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


@numba.jit(nopython=True)
def coord_conversion_coeffs(num_states, k):
    """coefficients for multi-dimensional coordinate conversion into 1D index"""
    return array([num_states ** (i - 1) for i in range(k, 0, -1)])


@numba.jit(nopython=True)
def coord_to_index(coord, coeffs):
    """converts a multi-dimensional coordinate into a 1D index"""
    return (coord * coeffs).sum()


@numba.jit(nopython=True)
def index_to_coord(index, coeffs):  # pragma: no cover
    """converts a 1D index into a multi-dimensional coordinate"""
    ndim = len(coeffs)
    coord = zeros(ndim, dtype=uint64)
    remainder = index
    for i in range(ndim):
        n, remainder = np_divmod(remainder, coeffs[i])
        coord[i] = n
    return coord


def indices_to_seqs(indices: ndarray, states: bytes, k: int) -> list[str]:
    """convert indices from k-dim into sequence

        Parameters
    ----------
    indices
        array of (len(states), k) dimensioned indices
    states
        the ordered characters, e.g. b"TCAG"
    """
    arr = indices_to_bytes(indices, states, k)
    return [bytearray(kmer).decode("utf8") for kmer in arr]


@numba.jit(nopython=True)
def indices_to_bytes(
    indices: ndarray, states: bytes, k: int
) -> ndarray:  # pragma: no cover
    """convert indices from k-dim into bytes

        Parameters
    ----------
    indices
        array of (len(states), k) dimensioned indices
    states
        the ordered characters, e.g. b"TCAG"
    k
        dimensions

    Raises
    -----
    IndexError if an index not in states

    Returns
    -------
    uint8 array.

    Notes
    -----
    Use indices_to_seqs to have the results returned as strings
    """
    result = zeros((len(indices), k), dtype=uint8)
    coeffs = coord_conversion_coeffs(len(states), k)
    num_states = len(states)
    for i in range(len(indices)):
        index = indices[i]
        coord = index_to_coord(index, coeffs)
        for j in range(k):
            if coord[j] >= num_states:
                raise IndexError("index out of character range")
            result[i][j] = states[coord[j]]

    return result


@numba.jit(nopython=True)
def kmer_indices(
    seq: ndarray, result: ndarray, num_states: int, k: int
) -> ndarray:  # pragma: no cover
    """return 1D indices for valid k-mers

    Parameters
    ----------
    seq
        numpy array of uint, assumed that canonical characters have
        sequential indexes which are all < num_states
    result
        array to be written into
    num_states
        defines range of possible ints at a position
    k
        k-mer size

    Returns
    -------
    result
    """
    coeffs = coord_conversion_coeffs(num_states, k)
    skip_until = 0
    for i in range(k):
        if seq[i] >= num_states:
            skip_until = i + 1

    result_idx = 0
    for i in range(len(result)):
        if seq[i + k - 1] >= num_states:
            skip_until = i + k

        if i < skip_until:
            continue

        index = (seq[i : i + k] * coeffs).sum()
        result[result_idx] = index
        result_idx += 1

    return result[:result_idx]


@numba.jit(nopython=True)
def kmer_counts(seq: ndarray, num_states: int, k: int) -> ndarray:  # pragma: no cover
    """return freqs of valid k-mers using 1D indices

    Parameters
    ----------
    seq
        numpy array of uint, assumed that canonical characters have
        sequential indexes which are all < num_states
    result
        array to be written into
    num_states
        defines range of possible ints at a position
    k
        k-mer size

    Returns
    -------
    result
    """
    coeffs = coord_conversion_coeffs(num_states, k)
    kfreqs = zeros(num_states**k, dtype=uint64)
    skip_until = 0
    for i in range(k):
        if seq[i] >= num_states:
            skip_until = i + 1

    for i in range(len(seq) - k + 1):
        if seq[i + k - 1] >= num_states:
            skip_until = i + k

        if i < skip_until:
            continue

        kmer = seq[i : i + k]
        index = (kmer * coeffs).sum()
        kfreqs[index] += 1

    return kfreqs


def _gt_zero(instance, attribute, value):
    if value <= 0:
        raise ValueError(f"must be > 0, not {value}")


@singledispatch
def _make_kcounts(data) -> vector:
    raise TypeError(f"type {type(data)} not supported")


@_make_kcounts.register
def _(data: ndarray):
    nonzero = {i: v for i, v in enumerate(data.tolist()) if v}
    return vector(
        vector_length=len(data), data=nonzero, dtype=_gettype(data.dtype.name)
    )


@_make_kcounts.register
def _(data: vector):
    return data


@dataclass(frozen=True)
class SeqArray:
    """A SeqArray stores an array of indices that map to the canonical characters
    of the moltype of the original sequence. Use divergent.util.arr2str() to
    recapitulate the original sequence."""

    seqid: str
    data: ndarray
    moltype: str
    source: str = None

    def __len__(self):
        return len(self.data)


@composable.define_app
def seq_to_seqarray(seq: c3_types.SeqType) -> SeqArray:
    as_indices = dv_utils.str2arr(moltype=seq.moltype)
    return SeqArray(
        seqid=seq.name,
        data=as_indices(str(seq)),
        moltype=seq.moltype,
        source=seq.source,
    )


@define(slots=True, order=True, hash=True)
class SeqRecord:
    """representation of a single sequence as kmer counts"""

    kcounts: vector = field(eq=False, converter=_make_kcounts)
    name: str = field(validator=validators.instance_of(str), eq=True)
    length: int = field(validator=[validators.instance_of(int), _gt_zero], eq=True)
    delta_jsd: float = field(
        init=False,
        validator=validators.instance_of(float),
        default=0.0,
        eq=False,
    )

    @property
    def size(self):
        return len(self.kfreqs)

    @property
    def entropy(self):
        return self.kfreqs.entropy

    @property
    def kfreqs(self):
        kcounts = self.kcounts.data
        kcounts = kcounts.astype(float)
        kfreqs = kcounts / kcounts.sum()
        return vector(data=kfreqs, vector_length=len(kfreqs), dtype=float)


class _seq_to_kmers:
    def __init__(self, k: int, moltype: str):
        """compute k-mers

        Parameters
        ----------
        k : int
            size of k-mers
        moltype : MolType
            cogent3 molecular type

        Raises
        ------
        ValueError
            if the mapping from characters to integers is not sequential

        Notes
        -----
        The sequence is converted to indices using states of canonical characters
        defined by moltype. Each k-mer is then a k-dimension coordinate. We
        convert those into a 1D coordinate. Use indices_to_seqs to convert
        indices back into k-mer sequences.
        """
        self.k = k
        self.canonical = _get_canonical_states(moltype)
        self.seq2array = dv_utils.str2arr(moltype=moltype)
        self.compress_pickled = dv_utils.pickle_data() + dv_utils.blosc_compress()


@composable.define_app
class seqarray_to_record(_seq_to_kmers):
    def main(self, seq: SeqArray) -> SeqRecord:
        kwargs = dict(
            vector_length=len(self.canonical) ** self.k,
            dtype=int,
            source=seq.source,
            name=seq.seqid,
        )
        counts = kmer_counts(seq.data, len(self.canonical), self.k)
        counts = {i: c for i, c in enumerate(counts) if c}

        kwargs["data"] = counts
        return SeqRecord(
            kcounts=vector(**kwargs), name=kwargs["name"], length=len(seq.data)
        )


def _get_canonical_states(moltype: str) -> bytes:
    moltype = get_moltype(moltype)
    canonical = list(moltype.alphabet)
    v = moltype.alphabet.to_indices(canonical)
    if not (0 <= min(v) < max(v) < len(canonical)):
        raise ValueError(f"indices of canonical states {canonical} not sequential {v}")
    return "".join(canonical).encode("utf8")


def _seq_to_all_kmers(seq: ndarray, states: bytes, k: int) -> ndarray:
    """return all valid k-mers from seq"""
    # positions with non-canonical characters are assigned value outside range
    num_states = len(states)
    dtype = get_array_type(num_states**k)

    # k-mers that include an index for ambiguity character are excluded
    result = zeros(len(seq) - k + 1, dtype=dtype)
    result = kmer_indices(seq, result, num_states, k)
    return result
