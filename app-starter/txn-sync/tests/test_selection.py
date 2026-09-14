from __future__ import annotations

from datetime import date

from conftest import WINDOW, make_transaction

from txn_sync.selection import Action, SelectionPolicy, SkipReason, decide

NOTHING_DONE = lambda *_: False  # noqa: E731


def test_an_ordinary_record_is_posted():
    decision = decide(make_transaction(), window=WINDOW, is_done=NOTHING_DONE)
    assert decision.action is Action.POST
    assert decision.should_post
    assert decision.reason is None


def test_already_synced_records_are_skipped():
    decision = decide(make_transaction(), window=WINDOW, is_done=lambda *_: True)
    assert decision.reason is SkipReason.ALREADY_SYNCED


def test_already_synced_is_checked_before_every_other_rule():
    """A record the destination already holds should say so.

    Reporting `zero_amount` for something that already crossed over sends
    someone looking for a bug that is not there.
    """
    zero = make_transaction(amount="0")
    decision = decide(zero, window=WINDOW, is_done=lambda *_: True)
    assert decision.reason is SkipReason.ALREADY_SYNCED


def test_records_outside_the_window_are_skipped():
    """The belt-and-braces check against an upstream ignoring date filters."""
    old = make_transaction(business_date=date(2025, 1, 1))
    decision = decide(old, window=WINDOW, is_done=NOTHING_DONE)
    assert decision.reason is SkipReason.OUTSIDE_WINDOW


def test_the_window_end_is_exclusive():
    on_end = make_transaction(business_date=WINDOW.end)
    assert decide(on_end, window=WINDOW, is_done=NOTHING_DONE).reason is (
        SkipReason.OUTSIDE_WINDOW
    )


def test_the_window_start_is_inclusive():
    on_start = make_transaction(business_date=WINDOW.start)
    assert decide(on_start, window=WINDOW, is_done=NOTHING_DONE).should_post


def test_zero_amount_is_skipped_by_default():
    decision = decide(
        make_transaction(amount="0.00"), window=WINDOW, is_done=NOTHING_DONE
    )
    assert decision.reason is SkipReason.ZERO_AMOUNT


def test_zero_amount_can_be_allowed_through():
    decision = decide(
        make_transaction(amount="0.00"),
        window=WINDOW,
        is_done=NOTHING_DONE,
        policy=SelectionPolicy(skip_zero_amount=False),
    )
    assert decision.should_post


def test_negative_amounts_are_posted():
    """Refunds and reversals are ordinary records, not an error case."""
    decision = decide(
        make_transaction(amount="-25.50"), window=WINDOW, is_done=NOTHING_DONE
    )
    assert decision.should_post


def test_an_empty_currency_allowlist_permits_everything():
    decision = decide(
        make_transaction(currency="XYZ"), window=WINDOW, is_done=NOTHING_DONE
    )
    assert decision.should_post


def test_a_populated_currency_allowlist_excludes_others():
    policy = SelectionPolicy(allowed_currencies=frozenset({"USD"}))
    assert (
        decide(
            make_transaction(currency="EUR"),
            window=WINDOW,
            is_done=NOTHING_DONE,
            policy=policy,
        ).reason
        is SkipReason.UNSUPPORTED_CURRENCY
    )
    assert decide(
        make_transaction(currency="USD"),
        window=WINDOW,
        is_done=NOTHING_DONE,
        policy=policy,
    ).should_post


def test_excluded_kinds_are_skipped():
    policy = SelectionPolicy(excluded_kinds=frozenset({"pending"}))
    assert (
        decide(
            make_transaction(kind="pending"),
            window=WINDOW,
            is_done=NOTHING_DONE,
            policy=policy,
        ).reason
        is SkipReason.EXCLUDED_KIND
    )


def test_a_missing_kind_is_not_excluded_by_the_kind_rule():
    """None must not be treated as a value that could match the exclude list."""
    policy = SelectionPolicy(excluded_kinds=frozenset({"pending"}))
    assert decide(
        make_transaction(kind=None),
        window=WINDOW,
        is_done=NOTHING_DONE,
        policy=policy,
    ).should_post


def test_is_done_receives_both_account_and_transaction_id():
    """Guards the account-scoping regression at the point rules consult it."""
    seen: list[tuple[str, str]] = []

    def record(account_id: str, transaction_id: str) -> bool:
        seen.append((account_id, transaction_id))
        return False

    decide(make_transaction("t1", "acct-7"), window=WINDOW, is_done=record)
    assert seen == [("acct-7", "t1")]
