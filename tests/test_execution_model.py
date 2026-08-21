import pytest

from app.onchain.execution_model import EVMExecutionModel, SolanaExecutionModel, build_execution_model


def test_solana_is_much_faster_than_ethereum():
    eth = EVMExecutionModel("eth")
    sol = SolanaExecutionModel()
    assert sol.estimate_inclusion().total_seconds < eth.estimate_inclusion().total_seconds


def test_bsc_is_faster_than_ethereum_but_slower_than_solana():
    eth = EVMExecutionModel("eth")
    bsc = EVMExecutionModel("bsc")
    sol = SolanaExecutionModel()
    assert sol.estimate_inclusion().total_seconds < bsc.estimate_inclusion().total_seconds < eth.estimate_inclusion().total_seconds


def test_build_execution_model_routes_solana_correctly():
    model = build_execution_model("solana")
    assert isinstance(model, SolanaExecutionModel)


def test_build_execution_model_routes_evm_chains_correctly():
    model = build_execution_model("eth")
    assert isinstance(model, EVMExecutionModel)
    assert model.chain == "eth"


def test_ethereum_inclusion_estimate_and_capturability_are_internally_consistent():
    model = EVMExecutionModel("eth")
    inclusion = model.estimate_inclusion()
    # broadcast 0.5 + mempool 0.5 + 1 block (12s) + half-block buffer (6s) = 19s
    assert inclusion.total_seconds == pytest.approx(19.0)
    assert model.is_capturable() is True  # 19s <= the documented 24s lifetime assumption


def test_a_chain_slower_than_its_own_lifetime_assumption_is_rejected():
    model = EVMExecutionModel("eth")
    model._EXPECTED_LIFETIME_SECONDS = {"eth": 5.0}  # deliberately unrealistic, to exercise the rejection branch
    assert model.is_capturable() is False


def test_solana_is_capturable():
    model = SolanaExecutionModel()
    assert model.is_capturable() is True


def test_inclusion_estimate_total_sums_all_four_components():
    model = SolanaExecutionModel()
    est = model.estimate_inclusion()
    assert est.total_seconds == pytest.approx(
        est.broadcast_latency_seconds + est.mempool_latency_seconds + est.block_inclusion_latency_seconds + est.confirmation_latency_seconds
    )
