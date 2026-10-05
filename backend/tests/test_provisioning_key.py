import re

from druks.sandbox.client import provisioning_key

# What Drukbox's Idempotency-Key header accepts.
ACCEPTED = re.compile(r"[A-Za-z0-9_\-:.]{1,255}")


def test_a_schedule_run_id_gives_a_key_that_drukbox_accepts():
    run_id = "sched-dependency_updates.find_tickets-trigger-2026-10-05T19:26:34.645715+00:00"
    key = provisioning_key(run_id, "step-1")
    assert ACCEPTED.fullmatch(key)
    assert key == provisioning_key(run_id, "step-1")


def test_keys_that_differ_only_in_a_refused_character_stay_different():
    plus = provisioning_key("sched-x-2026-10-05T19:26:34+00:00", "step")
    minus = provisioning_key("sched-x-2026-10-05T19:26:34-00:00", "step")
    assert plus != minus
    assert ACCEPTED.fullmatch(plus)


def test_an_accepted_key_stays_as_it_is():
    assert provisioning_key("wf-1", "workflow", "", "anthropic.1") == "wf-1:workflow:anthropic.1"


def test_a_long_key_is_cut_to_drukbox_s_limit():
    key = provisioning_key("w" * 300, "step")
    assert ACCEPTED.fullmatch(key)
    assert key != provisioning_key("w" * 301, "step")
