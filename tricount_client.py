"""Cliente mínimo para la API (no oficial) de Tricount / bunq.

Basado en https://github.com/mlaily/TricountApi.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

BASE_URL = "https://api.tricount.bunq.com"
# Constante que usa la app oficial; la API la exige.
CLIENT_REQUEST_ID = "049bfcdf-6ae4-4cee-af7b-45da31ea85d0"
# La API decide si la petición viene de la app de Tricount o de la de bunq según el User-Agent;
# con uno desconocido las escrituras fallan ("Group Expenses is no longer available in the bunq
# app"). Usamos el mismo que elrandar/tricount-api y mlaily/TricountApi. Se puede sobrescribir
# con la variable TRICOUNT_USER_AGENT.
USER_AGENT = os.environ.get("TRICOUNT_USER_AGENT", "com.bunq.tricount.android:RELEASE:7.0.7:3174:ANDROID:13:C")
SESSION_FILE = Path.home() / ".tricount-mcp" / "session.json"
CACHE_TTL_SECONDS = 60


def parse_key(tricount: str) -> str:
    """Acepta la key sola o un link (https://tricount.com/tXXXX, .../es/tXXXX, etc.)."""
    tricount = tricount.strip()
    match = re.search(r"tricount\.com/(?:[a-z]{2}/)?(?:tricount/)?([A-Za-z0-9]+)", tricount)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", tricount):
        return tricount
    raise ValueError(f"No reconozco '{tricount}' como key o link de Tricount.")


@dataclass
class Allocation:
    member: str
    amount: Decimal  # original currency, positive member share
    amount_registry: Decimal | None = None  # registry currency, avoids repeated FX rounding


@dataclass
class Entry:
    id: int
    description: str
    date: datetime
    paid_by: str
    amount: Decimal  # positivo = gasto
    currency: str
    amount_registry: Decimal  # en la moneda del tricount (para balances)
    type: str  # NORMAL (gasto), INCOME (ingreso), BALANCE (reembolso)
    category: str
    allocations: list[Allocation] = field(default_factory=list)


@dataclass
class Tricount:
    key: str
    id: int
    title: str
    description: str | None
    currency: str
    members: list[str]
    entries: list[Entry]
    member_uuids: dict[str, str] = field(default_factory=dict)  # nombre -> uuid de la membresía

    def balances(self) -> dict[str, Decimal]:
        """Positivo = le deben; negativo = debe."""
        bal = {m: Decimal(0) for m in self.members}
        for e in self.entries:
            bal[e.paid_by] = bal.get(e.paid_by, Decimal(0)) + e.amount_registry
            ratio = e.amount_registry / e.amount if e.amount else Decimal(1)
            for a in e.allocations:
                registry_share = a.amount_registry if a.amount_registry is not None else a.amount * ratio
                bal[a.member] = bal.get(a.member, Decimal(0)) - registry_share
        return {m: v.quantize(Decimal("0.01")) for m, v in bal.items()}

    def settlement(self) -> list[dict]:
        """Transferencias mínimas (greedy) para dejar todos los balances en cero."""
        bal = self.balances()
        debtors = sorted([[m, -v] for m, v in bal.items() if v < 0], key=lambda x: -x[1])
        creditors = sorted([[m, v] for m, v in bal.items() if v > 0], key=lambda x: -x[1])
        transfers = []
        i = j = 0
        while i < len(debtors) and j < len(creditors):
            amt = min(debtors[i][1], creditors[j][1])
            if amt > Decimal("0.004"):
                transfers.append({"from": debtors[i][0], "to": creditors[j][0], "amount": str(amt), "currency": self.currency})
            debtors[i][1] -= amt
            creditors[j][1] -= amt
            if debtors[i][1] <= Decimal("0.004"):
                i += 1
            if creditors[j][1] <= Decimal("0.004"):
                j += 1
        return transfers


def _name(membership: dict) -> str:
    inner = next(iter(membership.values()))
    return inner["alias"]["display_name"]


def _money(obj: dict) -> tuple[Decimal, str]:
    return Decimal(obj["value"]), obj["currency"]


def _parse(key: str, raw: dict) -> Tricount:
    reg = next((item["Registry"] for item in raw.get("Response", []) if "Registry" in item), None)
    if reg is None:  # la API responde 200 con una lista vacía si la key no existe
        raise ValueError(f"No existe un tricount con la key '{key}'. Revisa que el link esté completo.")
    currency = reg["currency"]
    entries = []
    for item in reg.get("all_registry_entry", []):
        e = item["RegistryEntry"]
        if e.get("status") == "DELETED":
            continue
        # Tricount API stores group-currency amounts in `amount` and the original
        # payment currency in `amount_local`. Older entries may omit amount_local.
        local_obj = e.get("amount_local") or e["amount"]
        local_amount, cur = _money(local_obj)
        registry_obj = next(
            (e[k] for k in ("amount", "amount_local")
             if e.get(k) and e[k]["currency"] == currency), None
        )
        if registry_obj is None:
            raise ValueError(f"Entry {e['id']}: missing group-currency amount ({currency})")
        registry_amount = Decimal(registry_obj["value"])
        allocations = []
        for a in e.get("allocations", []):
            local_part_obj = next(
                (a[k] for k in ("amount_local", "amount")
                 if a.get(k) and a[k]["currency"] == cur), None
            )
            registry_part_obj = next(
                (a[k] for k in ("amount", "amount_local")
                 if a.get(k) and a[k]["currency"] == currency), None
            )
            if local_part_obj is None or registry_part_obj is None:
                raise ValueError(f"Entry {e['id']}: allocation currency incomplete")
            local_part = -Decimal(local_part_obj["value"])
            registry_part = -Decimal(registry_part_obj["value"])
            if local_part != 0 or registry_part != 0:
                allocations.append(Allocation(
                    _name(a["membership"]), local_part, registry_part
                ))
        entries.append(Entry(
            id=e["id"],
            description=e.get("description") or "",
            date=datetime.fromisoformat(e["date"]),
            paid_by=_name(e["membership_owned"]),
            amount=-local_amount,
            currency=cur,
            amount_registry=-registry_amount,
            type=e.get("type_transaction", "NORMAL"),
            category=e.get("category_custom") or e.get("category") or "",
            allocations=allocations,
        ))
    entries.sort(key=lambda x: x.date)
    memberships = [next(iter(m.values())) for m in reg.get("memberships", [])]
    return Tricount(
        key=key,
        id=reg["id"],
        title=reg.get("title") or "",
        description=reg.get("description"),
        currency=currency,
        members=[m["alias"]["display_name"] for m in memberships],
        entries=entries,
        member_uuids={m["alias"]["display_name"]: m["uuid"] for m in memberships},
    )


class TricountClient:
    def __init__(self) -> None:
        self._http = httpx.Client(base_url=BASE_URL, timeout=30, headers={
            "User-Agent": USER_AGENT,
            "X-Bunq-Client-Request-Id": CLIENT_REQUEST_ID,
        })
        self._session: dict | None = None
        self._cache: dict[str, tuple[float, Tricount]] = {}
        self._joined: set[str] = set()

    # --- sesión -----------------------------------------------------------
    def _load_session(self) -> dict:
        if self._session:
            return self._session
        if SESSION_FILE.exists():
            self._session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            return self._session
        return self._register()

    def _register(self) -> dict:
        app_id = str(uuid.uuid4())
        pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
        ).decode()
        r = self._http.post(
            "/v1/session-registry-installation",
            headers={"app-id": app_id},
            json={"app_installation_uuid": app_id, "client_public_key": pub, "device_description": "Android"},
        )
        r.raise_for_status()
        items = r.json()["Response"]
        self._session = {
            "app_id": app_id,
            "token": next(i["Token"]["token"] for i in items if "Token" in i),
            "user_id": next(i["UserPerson"]["id"] for i in items if "UserPerson" in i),
        }
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(self._session), encoding="utf-8")
        return self._session

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        for attempt in range(2):
            s = self._load_session()
            r = self._http.request(method, path.format(user_id=s["user_id"]), params=params, json=json_body, headers={
                "app-id": s["app_id"],
                "X-Bunq-Client-Authentication": s["token"],
            })
            if r.status_code in (401, 403) and attempt == 0:
                self._register()  # token vencido: nos registramos de nuevo
                self._joined.clear()
                continue
            if r.is_error:
                raise RuntimeError(f"Tricount respondió {r.status_code}: {r.text[:500]}")
            return r.json()
        raise RuntimeError("No se pudo autenticar contra la API de Tricount.")

    # --- lectura ----------------------------------------------------------
    def get_tricount(self, tricount: str, fresh: bool = False) -> Tricount:
        key = parse_key(tricount)
        cached = self._cache.get(key)
        if not fresh and cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        raw = self._request("GET", "/v1/user/{user_id}/registry", params={"public_identifier_token": key})
        t = _parse(key, raw)
        self._cache[key] = (time.monotonic(), t)
        return t

    def get_exchange_rate(self, from_currency: str, to_currency: str) -> Decimal:
        """Get a Tricount-provided FX rate; fail closed if absent or invalid."""
        data = self._request("GET", "/v1/user/{user_id}/exchange-rate",
                             params={"currency": from_currency})
        for item in data.get("Response", []):
            rate = item.get("ExchangeRate", {})
            if rate.get("currency_target") == to_currency:
                value = Decimal(str(rate.get("rate", "")))
                if not value.is_finite() or value <= 0:
                    raise ValueError("Invalid exchange rate returned by Tricount")
                return value
        raise ValueError(f"No Tricount exchange rate for {from_currency} -> {to_currency}")

    # --- escritura --------------------------------------------------------
    def _join(self, key: str) -> None:
        """Sincroniza el tricount con nuestra sesión; la API lo exige antes de escribir."""
        if key in self._joined:
            return
        self._request("POST", "/v1/user/{user_id}/registry-synchronization", json_body={
            "all_registry_active": [{"public_identifier_token": key}],
            "all_registry_archived": [],
            "all_registry_deleted": [],
        })
        self._joined.add(key)

    def create_entry(self, t: Tricount, payload: dict) -> int:
        self._join(t.key)
        body = {"uuid": str(uuid.uuid4()), "status": "ACTIVE", **payload}
        r = self._request("POST", f"/v1/user/{{user_id}}/registry/{t.id}/registry-entry", json_body=body)
        self._cache.pop(t.key, None)
        return _extract_id(r)

    def delete_entry(self, t: Tricount, entry_id: int) -> None:
        self._join(t.key)
        self._request("DELETE", f"/v1/user/{{user_id}}/registry/{t.id}/registry-entry/{entry_id}")
        self._cache.pop(t.key, None)

    # Solo se usan en las pruebas, sobre tricounts creados por nosotros.
    def create_registry(self, title: str, currency: str, member_names: list[str]) -> str:
        r = self._request("POST", "/v1/user/{user_id}/registry", json_body={
            "title": title, "currency": currency, "description": "creado por tricount-mcp (prueba)"})
        reg_id = _extract_id(r)

        def fetch() -> dict:
            regs = self._request("GET", "/v1/user/{user_id}/registry")["Response"]
            return next(i["Registry"] for i in regs if "Registry" in i and i["Registry"]["id"] == reg_id)

        # Tricount crea un miembro por defecto ("tricount participant"): lo renombramos al primer
        # nombre y agregamos el resto.
        existing = [next(iter(m.values()))["uuid"] for m in fetch().get("memberships", [])]
        uuids = existing[: len(member_names)] + [str(uuid.uuid4()) for _ in member_names[len(existing):]]
        memberships = [
            {"uuid": u, "status": "ACTIVE", "auto_add_card_transaction": "", "setting": None,
             "alias": {"type": "UUID", "value": u, "name": name}}
            for u, name in zip(uuids, member_names)
        ]
        self._request("PUT", f"/v1/user/{{user_id}}/registry/{reg_id}", json_body={"memberships": memberships})
        return fetch()["public_identifier_token"]

    def delete_registry(self, t: Tricount) -> None:
        self._request("DELETE", f"/v1/user/{{user_id}}/registry/{t.id}")
        self._cache.pop(t.key, None)


def _extract_id(response: dict) -> int:
    for item in response.get("Response", []):
        if "Id" in item:
            return item["Id"]["id"]
    raise RuntimeError(f"Respuesta inesperada de Tricount: {response}")


ZERO_DECIMAL_CURRENCIES = {"CLP", "JPY", "KRW", "PYG", "ISK", "VND", "COP", "HUF", "TWD", "UGX", "XAF", "XOF"}


def split_amount(total: Decimal, weights: list[Decimal], currency: str) -> list[Decimal]:
    """Reparte `total` según `weights` en unidades mínimas de la moneda, sin perder ni sobrar
    nada: el resto se asigna uno a uno a los primeros miembros."""
    unit = Decimal(1) if currency in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
    units = int((total / unit).to_integral_value())
    wsum = sum(weights)
    raw = [units * w / wsum for w in weights]
    parts = [int(x) for x in raw]  # truncado
    leftover = units - sum(parts)
    for i in sorted(range(len(parts)), key=lambda i: raw[i] - parts[i], reverse=True)[:leftover]:
        parts[i] += 1
    return [Decimal(p) * unit for p in parts]


def fmt(value: Decimal, currency: str) -> str:
    return str(value.quantize(Decimal(1) if currency in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")))
