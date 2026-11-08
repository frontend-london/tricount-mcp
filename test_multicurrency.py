"""Offline unit tests for mixed-currency Tricount MCP. No network, no live writes."""
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import server
from tricount_client import Tricount, _parse

class DummyClient:
    def __init__(self):
        self.t = Tricount("test-key", 42, "Test", "", "PLN",
                          ["Piotruś", "Ale *"], [],
                          {"Piotruś": "uuid-p", "Ale *": "uuid-a"})
    def get_tricount(self, _, fresh=False):
        return self.t
    def get_exchange_rate(self, frm, to):
        assert to == "PLN"
        return {"TRY": Decimal("0.08"), "EUR": Decimal("4.50"), "USD": Decimal("4")}[frm]

def payload_for(amount, currency, **kwargs):
    fake = DummyClient()
    def intercept(t, payload, preview, confirm, acting_as, action):
        return {"payload": payload, "preview": preview}
    with patch.object(server, "client", fake), patch.object(server, "_save_or_preview", intercept):
        return server.create_expense(
            acting_as="Piotruś", paid_by="Piotruś", description="test wtyczki",
            amount=amount, currency=currency, tricount="test-key", split_among=["Piotruś","Ale *"],
            expense_date=date(2026,11,8), **kwargs)

def membership(name):
    return {"Membership": {"alias": {"display_name": name}}}

def make_registry(raw_amount="0.08", local_amount="1.00"):
    return {"Response": [{"Registry": {
        "id": 42, "title":"Test", "description":"", "currency":"PLN",
        "memberships": [{"Membership": {"alias": {"display_name": n}, "uuid": uid}}
                        for n,uid in [("Piotruś","uuid-p"),("Ale *","uuid-a")]],
        "all_registry_entry": [{"RegistryEntry": {
            "id":987, "status":"ACTIVE", "date":"2026-11-08 10:00:00",
            "description":"test wtyczki", "type_transaction":"NORMAL",
            "category":"TRANSPORT", "membership_owned":membership("Piotruś"),
            "amount":{"value":f"-{raw_amount}","currency":"PLN"},
            "amount_local":{"value":f"-{local_amount}","currency":"TRY"},
            "allocations":[
                {"membership":membership(n),
                 "amount":{"value":"-0.04","currency":"PLN"},
                 "amount_local":{"value":"-0.50","currency":"TRY"}}
                for n in ("Piotruś","Ale *")]
        }}]
    }}]}

class FXTests(unittest.TestCase):
    def test_one_try_preserves_original_and_converts_balances(self):
        o=payload_for(1,"TRY")
        p=o["payload"]; a=p["allocations"]
        self.assertEqual(p["amount"],{"value":"-0.08","currency":"PLN"})
        self.assertEqual(p["amount_local"],{"value":"-1.00","currency":"TRY"})
        self.assertEqual(p["exchange_rate"],"0.08")
        self.assertEqual([x["amount"]["value"] for x in a],["-0.04","-0.04"])
        self.assertEqual([x["amount_local"]["value"] for x in a],["-0.50","-0.50"])
        self.assertEqual(o["preview"]["reparto"],{"Piotruś":"0.50","Ale *":"0.50"})

    def test_usd_and_eur(self):
        for amount, cur, expect in [(114.56,"USD","458.24"),(68.68,"EUR","309.06")]:
            with self.subTest(cur=cur):
                o=payload_for(amount,cur)
                self.assertEqual(o["preview"]["monto"],f"{amount:.2f}")
                self.assertEqual(o["preview"]["moneda"],cur)
                self.assertEqual(o["preview"]["kwota_w_walucie_grupy"],expect)

    def test_odd_cent_distribution(self):
        o=payload_for(29.61,"USD")
        p=o["payload"]
        self.assertEqual(p["amount_local"]["value"],"-29.61")
        self.assertEqual(sum(Decimal(x["amount_local"]["value"]) for x in p["allocations"]),Decimal("-29.61"))
        self.assertEqual(sum(Decimal(x["amount"]["value"]) for x in p["allocations"]),Decimal("-118.44"))

    def test_pln_backward_compat(self):
        o=payload_for(80,"PLN")
        p=o["payload"]
        self.assertNotIn("amount_local",p)
        self.assertNotIn("exchange_rate",p)
        self.assertEqual(p["allocations"][0]["amount"]["currency"],"PLN")
        self.assertEqual(o["preview"]["reparto"],{"Piotruś":"40.00","Ale *":"40.00"})

    def test_fail_closed_on_bad_input(self):
        for amount, cur, rate in [(1,"TRY",-1),(1,"TRY",0),(1,"TRY",float("inf")),(0,"TRY",None),(1.001,"TRY",None),(1,"XX",None)]:
            with self.subTest(cur=cur,amount=amount,rate=rate):
                with self.assertRaises(Exception):
                    payload_for(amount,cur,exchange_rate=rate)

    def test_original_currency_reader_and_exact_balances(self):
        t=_parse("test-key",make_registry())
        self.assertEqual(t.entries[0].amount,Decimal("1.00"))
        self.assertEqual(t.entries[0].currency,"TRY")
        self.assertEqual(t.entries[0].amount_registry,Decimal("0.08"))
        self.assertEqual(t.entries[0].allocations[0].amount,Decimal("0.50"))
        self.assertEqual(t.entries[0].allocations[0].amount_registry,Decimal("0.04"))
        self.assertEqual(t.balances(),{"Piotruś":Decimal("0.04"),"Ale *":Decimal("-0.04")})
        self.assertEqual(server._entry_dict(t.entries[0])["moneda"],"TRY")

    def test_old_pln_reader(self):
        raw=make_registry()
        e=raw["Response"][0]["Registry"]["all_registry_entry"][0]["RegistryEntry"]
        e.pop("amount_local")
        e["amount"]={"value":"-80.00","currency":"PLN"}
        for a in e["allocations"]:
            a.pop("amount_local")
            a["amount"]={"value":"-40.00","currency":"PLN"}
        t=_parse("test-key",raw)
        self.assertEqual(t.entries[0].currency,"PLN")
        self.assertEqual(t.balances(),{"Piotruś":Decimal("40.00"),"Ale *":Decimal("-40.00")})

if __name__=="__main__":
    unittest.main(verbosity=2)
