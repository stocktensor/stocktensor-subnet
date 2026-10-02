from __future__ import annotations

import pytest
from bittensor.sp_core import Keypair
from fakes import FakeRpc


@pytest.fixture(scope="session")
def alice() -> Keypair:
    return Keypair.create_from_uri("//Alice")


@pytest.fixture(scope="session")
def bob() -> Keypair:
    return Keypair.create_from_uri("//Bob")


@pytest.fixture(scope="session")
def charlie() -> Keypair:
    return Keypair.create_from_uri("//Charlie")


@pytest.fixture
def fake_rpc() -> FakeRpc:
    return FakeRpc()
