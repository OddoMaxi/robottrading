from unittest.mock import AsyncMock, patch

import pytest

from app.onchain.gas_engine import RpcGasProvider


@pytest.mark.asyncio
async def test_solana_gas_estimate_needs_no_network_and_uses_the_fixed_signature_fee():
    provider = RpcGasProvider()
    estimate = await provider.estimate_gas_cost_usd("solana", native_token_price_usd=150.0)
    # 5000 lamports/signature * 2 signatures = 10000 lamports = 0.00001 SOL
    assert estimate.gas_cost_native == pytest.approx(0.00001)
    assert estimate.gas_cost_usd == pytest.approx(0.00001 * 150.0)
    assert estimate.chain == "solana"


@pytest.mark.asyncio
async def test_solana_atomic_multi_leg_costs_more_than_a_simple_swap():
    provider = RpcGasProvider()
    simple = await provider.estimate_gas_cost_usd("solana", native_token_price_usd=150.0, complexity="simple_swap")
    atomic = await provider.estimate_gas_cost_usd("solana", native_token_price_usd=150.0, complexity="atomic_multi_leg")
    assert atomic.gas_cost_usd > simple.gas_cost_usd


@pytest.mark.asyncio
async def test_evm_gas_cost_scales_with_real_fetched_gas_price():
    provider = RpcGasProvider()
    with patch("app.onchain.gas_engine._fetch_evm_gas_price_wei", new=AsyncMock(return_value=20_000_000_000.0)):  # 20 gwei
        estimate = await provider.estimate_gas_cost_usd("eth", native_token_price_usd=2_500.0)
    # 20 gwei * 180,000 gas units = 0.0036 ETH
    assert estimate.gas_cost_native == pytest.approx(0.0036)
    assert estimate.gas_cost_usd == pytest.approx(0.0036 * 2_500.0)


@pytest.mark.asyncio
async def test_evm_gas_fetch_failure_raises_rather_than_fabricating_a_cost():
    provider = RpcGasProvider()
    with patch("app.onchain.gas_engine._fetch_evm_gas_price_wei", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError):
            await provider.estimate_gas_cost_usd("eth", native_token_price_usd=2_500.0)


@pytest.mark.asyncio
async def test_multi_hop_evm_gas_costs_more_than_a_simple_swap():
    provider = RpcGasProvider()
    with patch("app.onchain.gas_engine._fetch_evm_gas_price_wei", new=AsyncMock(return_value=20_000_000_000.0)):
        simple = await provider.estimate_gas_cost_usd("eth", native_token_price_usd=2_500.0, complexity="simple_swap")
        multi_hop = await provider.estimate_gas_cost_usd("eth", native_token_price_usd=2_500.0, complexity="multi_hop")
    assert multi_hop.gas_cost_usd > simple.gas_cost_usd
