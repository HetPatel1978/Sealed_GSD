from sealed.core import breakdown as bd
from sealed.core.models import Decision
from sealed.fo import messages
from tests.helpers import job

TEXT = ("Out-the-door total: $30,980. Vehicle $28,280, doc fee $449, nitrogen-filled tires $199, sales tax $1,709, "
        "title and registration $343.")
RAW = {"vehicle_price": 28280, "dealer_fees": [{"label": "Doc fee", "amount": 449}],
       "add_ons": [{"label": "Nitrogen-filled tires", "amount": 199}], "taxes": 1709, "title_registration": 343,
       "other": [], "out_the_door_total": 30980, "valid_until": None}


def test_grounded_breakdown_that_adds_up_has_no_flags():
    clean, flags = bd.validate(RAW, TEXT, 30980.0)
    assert flags == [] and clean["add_ons"] == [{"label": "Nitrogen-filled tires", "amount": 199.0}]


def test_parts_that_do_not_add_up_are_flagged_and_total_is_kept():
    text = TEXT.replace("$1,709", "$1,673")
    clean, flags = bd.validate(dict(RAW, taxes=1673), text, 30980.0)
    assert "breakdown_mismatch" in flags and clean["out_the_door_total"] == 30980.0


def test_within_one_dollar_is_fine():
    text = TEXT.replace("$343", "$342.50")
    assert bd.validate(dict(RAW, title_registration=342.5), text, 30980.0)[1] == []


def test_ungrounded_amounts_are_dropped_and_flagged():
    clean, flags = bd.validate(dict(RAW, add_ons=[{"label": "Paint protection", "amount": 899}]), TEXT, 30980.0)
    assert "breakdown_ungrounded" in flags and clean["add_ons"] == []


def test_no_breakdown_is_fine():
    assert bd.validate(None, TEXT, 30980.0) == (None, [])


def test_message_numbers_say_where_every_number_came_from():
    j = job()
    m = messages.build(j, Decision("a", "counter", "counter", round=2, offer=j.target, own_price=30690.0,
                                   own_quote_id="q_05"), "m_01", "SEALED-T0000001", "buyer@x.example")
    nums = messages.number_provenance(m, j)
    assert {"value": 30690.0, "source": "ledger", "quote_id": "q_05"} in nums
    assert {"value": j.target, "source": "target"} in nums
    assert all(n["source"] != "unknown" for n in nums)
