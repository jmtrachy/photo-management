"""Shared fixtures for the database-module tests.

Each test module opts in by declaring a module-level ``db_module`` pointing at
the data-access module under test, e.g. ``db_module = albums``. The fixtures
below then patch that module's boto3 handles so no real AWS calls happen.
"""
import pytest
from unittest.mock import patch


def _table_attr(module):
    """The module's single ``*_table`` handle (e.g. ``albums_table``)."""
    return next(name for name in vars(module) if name.endswith("_table"))


@pytest.fixture
def mock_dynamo_table(request):
    """Mock the table handle of the module under test.

    ``.name`` is preset to the conventional test table name so batch tests that
    read ``mock_dynamo_table.name`` don't have to set it themselves.
    """
    module = request.module.db_module
    attr = _table_attr(module)
    with patch.object(module, attr) as mock_table:
        mock_table.name = "test-" + attr.replace("_", "-")
        yield mock_table


@pytest.fixture
def dynamo(request):
    """Mock the shared ``dynamodb`` resource (used for batch_get_item)."""
    module = request.module.db_module
    with patch.object(module, "dynamodb") as mock_dynamodb:
        yield mock_dynamodb
