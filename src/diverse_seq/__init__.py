"""diverse_seq: a tool for sampling diverse biological sequences"""

# need to import hdf5plugin here to make sure the plugin path can be
# found by h5py
import hdf5plugin  # noqa
import typing

if typing.TYPE_CHECKING:
    from cogent3.core.new_alignment import SequenceCollection

__version__ = "2024.12.26a2"


def load_sample_data() -> "SequenceCollection":
    """load sample data"""
    from cogent3 import load_aligned_seqs

    from .util import get_sample_data_path

    path = get_sample_data_path()
    return load_aligned_seqs(path, moltype="dna", new_type=True).degap()
