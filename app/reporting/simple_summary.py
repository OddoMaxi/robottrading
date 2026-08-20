"""Simple Mode aggregations (Dashboard Simple V4 spec).

Read-only rollups built on tables the engine already writes — no trading
logic lives here. Two kinds of function: DB-backed builders (async, need a
session) and small pure functions (state classification, drawdown) kept
separate so they can be unit tested without a database.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import PRIORITY_EXCHANGES
from app.database.models import OpportunityRecord, PriceSnapshot, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES

# Price data older than this means that exchange's feed has likely stalled
# (collectors push on every tick) rather than the market just being quiet —
# same freshness assumption the False Opportunity Filter uses per-quote,
# applied here per-exchange for the header's connection indicator.
EXCHANGE_STALE_AFTER_SECONDS = 30.0
# No opportunity detected at all in this long means the detection loop
# itself is probably stuck — not just "no edge right now", which happens
# constantly and is normal (see EXCHANGE_STALE_AFTER_SECONDS for that check).
ENGINE_STALE_AFTER_SECONDS = 120.0


class RobotHealth(StrEnum):
    RUNNING = "running"  # 🟢 EN MARCHE
    DEGRADED = "degraded"  # 🟡 SURVEILLANCE
    DOWN = "down"  # 🔴 PROBLÈME


def classify_robot_health(last_opportunity_age_seconds: float | None, exchanges_connected: dict[str, bool]) -> RobotHealth:
    if last_opportunity_age_seconds is None:
        return RobotHealth.DEGRADED  # just started, no data yet — not necessarily broken
    if last_opportunity_age_seconds > ENGINE_STALE_AFTER_SECONDS or not any(exchanges_connected.values()):
        return RobotHealth.DOWN
    if not all(exchanges_connected.values()):
        return RobotHealth.DEGRADED
    return RobotHealth.RUNNING


@dataclass(slots=True)
class RobotStatus:
    health: RobotHealth
    exchanges_connected: dict[str, bool]
    last_opportunity_age_seconds: float | None


async def build_robot_status(session: AsyncSession, now: datetime | None = None) -> RobotStatus:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    latest_opp = (
        await session.execute(select(OpportunityRecord.detected_at).order_by(OpportunityRecord.detected_at.desc()).limit(1))
    ).scalar_one_or_none()
    last_opportunity_age_seconds = (now - latest_opp).total_seconds() if latest_opp else None

    exchanges_connected: dict[str, bool] = {}
    for exchange in PRIORITY_EXCHANGES:
        latest_tick = (
            await session.execute(
                select(PriceSnapshot.recorded_at)
                .where(PriceSnapshot.exchange == exchange)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        exchanges_connected[exchange] = latest_tick is not None and (now - latest_tick).total_seconds() <= EXCHANGE_STALE_AFTER_SECONDS

    return RobotStatus(
        health=classify_robot_health(last_opportunity_age_seconds, exchanges_connected),
        exchanges_connected=exchanges_connected,
        last_opportunity_age_seconds=last_opportunity_age_seconds,
    )


async def build_portfolio_capital(session: AsyncSession, portfolio_id: int, initial_capital_usd: float) -> float:
    """Current capital = starting capital + every trade's net P&L, all-time.

    Live balances aren't persisted per-tick (they live in the engine's
    in-memory VirtualPortfolio) — this reconstructs the same number from the
    trade ledger, which *is* persisted, instead of adding a new write path.
    """
    net_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0)).where(
                SimulatedTradeRecord.portfolio_id == portfolio_id, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES)
            )
        )
    ).scalar() or 0.0
    return initial_capital_usd + float(net_pnl)


@dataclass(slots=True)
class EquityPoint:
    at: datetime
    capital_usd: float


async def build_equity_curve(
    session: AsyncSession, portfolio_id: int, initial_capital_usd: float, hours: float = 24.0, now: datetime | None = None
) -> list[EquityPoint]:
    """Capital over time, reconstructed as a running total from the trade
    ledger — the one chart Simple Mode's home screen shows (spec section 14)."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    period_start = now - timedelta(hours=hours)

    # Capital already earned/lost *before* the window, so the curve starts
    # at the right level instead of resetting to initial_capital_usd at
    # period_start.
    pre_window_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0)).where(
                SimulatedTradeRecord.portfolio_id == portfolio_id,
                SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                SimulatedTradeRecord.executed_at < period_start,
            )
        )
    ).scalar() or 0.0

    rows = (
        await session.execute(
            select(SimulatedTradeRecord.executed_at, SimulatedTradeRecord.net_profit_usd)
            .where(
                SimulatedTradeRecord.portfolio_id == portfolio_id,
                SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                SimulatedTradeRecord.executed_at >= period_start,
            )
            .order_by(SimulatedTradeRecord.executed_at.asc())
        )
    ).all()

    running = initial_capital_usd + float(pre_window_pnl)
    points = [EquityPoint(at=period_start, capital_usd=running)]
    for executed_at, net_profit_usd in rows:
        running += float(net_profit_usd)
        points.append(EquityPoint(at=executed_at, capital_usd=running))
    return points


@dataclass(slots=True)
class RobotStateMessage:
    tone: str  # "good" | "warn" | "bad" — drives the card's color (spec section 18)
    title: str
    body: str


def pick_robot_state_message(robot: RobotStatus, opportunities_today: int, profitable_today: int) -> RobotStateMessage:
    """ÉTAT DU ROBOT (spec section 8) — turns technical signals into one of
    a handful of plain-language states, worst-first: a broken feed always
    outranks "just not many opportunities today"."""
    if robot.health == RobotHealth.DOWN:
        disconnected = [name for name, ok in robot.exchanges_connected.items() if not ok]
        names = ", ".join(name.capitalize() for name in disconnected) if disconnected else "Une plateforme"
        return RobotStateMessage(
            "bad",
            "🔴 Problème technique",
            f"{names} ne répond plus correctement. Le robot a mis les simulations en pause par sécurité.",
        )
    if robot.health == RobotHealth.DEGRADED:
        disconnected = [name for name, ok in robot.exchanges_connected.items() if not ok]
        if disconnected:
            names = ", ".join(name.capitalize() for name in disconnected)
            return RobotStateMessage("warn", "🟡 Surveillance", f"{names} répond plus lentement que d'habitude. Le robot continue de surveiller le marché.")
        return RobotStateMessage("warn", "🟡 Surveillance", "Le robot démarre — pas encore assez de données pour confirmer que tout fonctionne normalement.")
    if opportunities_today == 0:
        return RobotStateMessage("warn", "🟡 Opportunités faibles", "Peu d'opportunités intéressantes détectées pour l'instant. Le robot continue de surveiller le marché.")
    if profitable_today == 0:
        return RobotStateMessage(
            "warn",
            "🟡 Frais trop élevés",
            f"{opportunities_today} opportunité(s) repérée(s) aujourd'hui, mais les frais étaient à chaque fois supérieurs aux gains. Le robot continue de surveiller.",
        )
    return RobotStateMessage("good", "🟢 Tout va bien", "Le robot fonctionne normalement. Il surveille le marché en continu et ne détecte aucun problème.")


def build_explainer_narrative(detected: int, profitable: int, executed: int, net_pnl_usd: float) -> str:
    """ROBOT EXPLIQUE (spec section 24) — the day's opportunity funnel as
    one plain-language paragraph instead of separate technical counters."""
    if detected == 0:
        return "Le robot n'a pas encore trouvé d'opportunité aujourd'hui. Il continue de surveiller le marché."
    ignored = detected - profitable
    lines = [f"Le robot a trouvé {detected} opportunité{'s' if detected != 1 else ''} aujourd'hui."]
    if ignored > 0:
        lines.append(f"{ignored} {'ont' if ignored != 1 else 'a'} été ignorée{'s' if ignored != 1 else ''} car les frais étaient trop élevés.")
    lines.append(f"{profitable} {'ont' if profitable != 1 else 'a'} passé les critères.")
    if executed > 0:
        lines.append(f"{executed} {'ont' if executed != 1 else 'a'} été exécutée{'s' if executed != 1 else ''} en simulation.")
    lines.append(f"Résultat net : {net_pnl_usd:+.2f} $.")
    return " ".join(lines)


def compute_max_drawdown_usd(capital_values: list[float]) -> float:
    """Largest peak-to-trough drop across a capital curve, as a negative $
    amount (0.0 if the curve never dropped below a prior peak) — Simple
    Mode's "Plus forte baisse" (spec section 25)."""
    if not capital_values:
        return 0.0
    peak = capital_values[0]
    max_drawdown = 0.0
    for value in capital_values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value - peak)
    return max_drawdown


@dataclass(slots=True)
class TradeRow:
    id: int
    executed_at: datetime
    symbol: str
    strategy: str
    capital_usd: float
    gross_profit_usd: float
    fees_usd: float
    net_profit_usd: float
    holding_period_seconds: float | None
    won: bool


async def list_recent_trades(session: AsyncSession, portfolio_id: int, limit: int = 50) -> list[TradeRow]:
    rows = (
        await session.execute(
            select(
                SimulatedTradeRecord.id,
                SimulatedTradeRecord.executed_at,
                OpportunityRecord.symbol,
                OpportunityRecord.strategy,
                SimulatedTradeRecord.capital_usd,
                SimulatedTradeRecord.gross_profit_usd,
                SimulatedTradeRecord.fees_usd,
                SimulatedTradeRecord.net_profit_usd,
                OpportunityRecord.holding_period_seconds,
            )
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(SimulatedTradeRecord.portfolio_id == portfolio_id, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES))
            .order_by(SimulatedTradeRecord.executed_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        TradeRow(
            id=trade_id,
            executed_at=executed_at,
            symbol=symbol,
            strategy=strategy,
            capital_usd=float(capital_usd),
            gross_profit_usd=float(gross_profit_usd),
            fees_usd=float(fees_usd),
            net_profit_usd=float(net_profit_usd),
            holding_period_seconds=float(holding_period_seconds) if holding_period_seconds is not None else None,
            won=float(net_profit_usd) > 0,
        )
        for trade_id, executed_at, symbol, strategy, capital_usd, gross_profit_usd, fees_usd, net_profit_usd, holding_period_seconds in rows
    ]
