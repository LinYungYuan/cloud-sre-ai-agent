import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from scripts.contract_check.check_contracts import validate_all


def test_all_contracts_and_examples_are_valid():
    validate_all(Path(__file__).parents[2])
