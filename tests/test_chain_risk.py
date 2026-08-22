from unittest.mock import AsyncMock, patch

import pytest

from app.onchain.chain_risk import ChainHealth, check_chain_health, classify_evm_chain_health


def test_no_gas_price_data_means_unavailable():
    assert classify_evm_chain_health("eth", None) == ChainHealth.UNAVAILABLE


def test_normal_gas_price_is_healthy():
    assert classify_evm_chain_health("eth", 15.0) == ChainHealth.HEALTHY


def test_elevated_gas_price_is_congested():
    assert classify_evm_chain_health("eth", 50.0) == ChainHealth.CONGESTED


def test_severely_elevated_gas_price_is_degraded():
    assert classify_evm_chain_health("eth", 150.0) == ChainHealth.DEGRADED


def test_bsc_has_its_own_lower_thresholds_than_ethereum():
    # 50 gwei is merely "congested" on Ethereum but severely elevated on BSC's normally-much-cheaper baseline.
    assert classify_evm_chain_health("bsc", 50.0) == ChainHealth.DEGRADED


@pytest.mark.asyncio
async def test_check_chain_health_evm_uses_the_real_fetched_gas_price():
    with patch("app.onchain.chain_risk._fetch_evm_gas_price_wei", new=AsyncMock(return_value=20_000_000_000.0)):  # 20 gwei
        health = await check_chain_health("eth")
    assert health == ChainHealth.HEALTHY


@pytest.mark.asyncio
async def test_check_chain_health_evm_fetch_failure_is_unavailable():
    with patch("app.onchain.chain_risk._fetch_evm_gas_price_wei", new=AsyncMock(return_value=None)):
        health = await check_chain_health("eth")
    assert health == ChainHealth.UNAVAILABLE


@pytest.mark.asyncio
async def test_check_chain_health_solana_reachable_is_healthy():
    with patch("app.onchain.chain_risk._check_solana_reachable", new=AsyncMock(return_value=True)):
        health = await check_chain_health("solana")
    assert health == ChainHealth.HEALTHY


@pytest.mark.asyncio
async def test_check_chain_health_solana_unreachable_is_unavailable():
    with patch("app.onchain.chain_risk._check_solana_reachable", new=AsyncMock(return_value=False)):
        health = await check_chain_health("solana")
    assert health == ChainHealth.UNAVAILABLE
