"""Acesso a dados de clientes. O resto da aplicação só conhece o `CustomerRepository`;
trocar as fixtures por uma API real exige apenas outra implementação deste protocolo."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class Receivable(BaseModel):
    id: str
    sale_date: date
    amount: Decimal
    currency: str
    settlement_date: date
    status: Literal["previsto", "pago", "bloqueado"]


class TransactionInfo(BaseModel):
    status: str
    decline_code: str | None
    amount: Decimal
    currency: str
    occurred_at: datetime


class Terminal(BaseModel):
    id: str
    model: str
    connection_state: Literal["online", "offline"]
    last_error_code: str | None
    observed_at: datetime
    last_transaction: TransactionInfo | None


class CustomerRepository(Protocol):
    def customer_exists(self, customer_id: str) -> bool: ...

    def receivables_for(
        self, customer_id: str, sales_date: date, today: date
    ) -> list[Receivable]: ...

    def terminals_for(self, customer_id: str, now: datetime) -> list[Terminal]: ...


class JsonCustomerRepository:
    """Fixtures sintéticas. Datas são offsets relativos ao relógio, para a demo valer em
    qualquer dia e os testes serem determinísticos."""

    def __init__(self, path: str | Path) -> None:
        self._data: dict[str, dict[str, Any]] = json.loads(Path(path).read_text("utf-8"))

    def customer_exists(self, customer_id: str) -> bool:
        return customer_id in self._data

    def receivables_for(self, customer_id: str, sales_date: date, today: date) -> list[Receivable]:
        rows: list[Receivable] = []
        for raw in self._data[customer_id]["receivables"]:
            sale_date = today + timedelta(days=raw["sale_offset_days"])
            if sale_date != sales_date:
                continue
            rows.append(
                Receivable(
                    id=raw["id"],
                    sale_date=sale_date,
                    amount=Decimal(raw["amount"]),
                    currency=raw["currency"],
                    settlement_date=sale_date + timedelta(days=raw["settlement_offset_days"]),
                    status=raw["status"],
                )
            )
        return rows

    def terminals_for(self, customer_id: str, now: datetime) -> list[Terminal]:
        terminals: list[Terminal] = []
        for raw in self._data[customer_id]["terminals"]:
            tx = raw["last_transaction"]
            terminals.append(
                Terminal(
                    id=raw["id"],
                    model=raw["model"],
                    connection_state=raw["connection_state"],
                    last_error_code=raw["last_error_code"],
                    observed_at=now - timedelta(minutes=raw["observed_minutes_ago"]),
                    last_transaction=None
                    if tx is None
                    else TransactionInfo(
                        status=tx["status"],
                        decline_code=tx["decline_code"],
                        amount=Decimal(tx["amount"]),
                        currency=tx["currency"],
                        occurred_at=now - timedelta(minutes=tx["minutes_ago"]),
                    ),
                )
            )
        return terminals
