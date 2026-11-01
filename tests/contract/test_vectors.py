"""Runs every vector in ``protocol/vectors/`` against the simulator (T032).

Each vector is one test, named by its id. The same files must pass, unchanged, against
the real firmware later; only the adapter differs.
"""

import pytest
from simulator_adapter import SimulatorAdapter
from vector_runner import format_failure, load_vector_files, run_vector

_VECTORS = load_vector_files()


@pytest.mark.parametrize(
    "vector",
    [pytest.param(vector, id=f"{file}::{vector['id']}") for file, vector in _VECTORS],
)
def test_vector(vector):
    result = run_vector(vector, SimulatorAdapter())
    assert result.passed, format_failure(result)
