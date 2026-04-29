from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import session_scope
from app.main import (
    collect_balance_tao_from_events,
    collect_settlement_tao_from_events,
    collect_tao_amount_candidates,
    normalized_trade_amount_tao,
)
from app.models import ChainEvent


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python scripts/inspect_chain_event.py <区块号> <event_index>")
        return 1

    block_number = int(sys.argv[1])
    event_index = int(sys.argv[2])

    with session_scope() as session:
        row = session.query(ChainEvent).filter(
            ChainEvent.block_number == block_number,
            ChainEvent.event_index == event_index,
        ).first()

    if row is None:
        print("没找到对应记录")
        return 2

    try:
        raw = json.loads(row.raw_payload or "{}")
    except Exception as exc:
        print(f"raw_payload JSON 解析失败: {exc}")
        print(row.raw_payload)
        return 3

    related_events = raw.get("related_events", []) if isinstance(raw, dict) else []
    action_type = str(raw.get("action_type") or row.action_type or "") if isinstance(raw, dict) else str(row.action_type or "")

    settlement = collect_settlement_tao_from_events(action_type, related_events)
    balance = collect_balance_tao_from_events(action_type, related_events)
    direct = []
    if isinstance(related_events, list):
        for event in related_events:
            direct.extend(collect_tao_amount_candidates(event))

    print(f"block_number = {row.block_number}")
    print(f"event_index = {row.event_index}")
    print(f"action_type = {row.action_type}")
    print(f"call_name = {row.call_name}")
    print(f"stored_amount_tao = {row.amount_tao}")
    print(f"recomputed_amount_tao = {normalized_trade_amount_tao(row)}")
    print(f"settlement_candidates = {settlement}")
    print(f"balance_candidates = {balance}")
    print(f"direct_event_candidates = {direct}")
    print("raw_payload =")
    print(json.dumps(raw, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
