from __future__ import annotations

from datetime import date

import pytest

from txn_sync.keys import KEY_VERSION, checkpoint_id, idempotency_key

DAY = date(2026, 3, 3)


def key(**overrides: object) -> str:
    args: dict[str, object] = {
        "source_system": "src",
        "account_id": "acct-1",
        "transaction_id": "t1",
        "business_date": DAY,
    }
    args.update(overrides)
    return idempotency_key(**args)  # type: ignore[arg-type]


def test_same_record_always_produces_the_same_key():
    """The property the whole retry story depends on.

    If this ever fails, a re-run stops being free and becomes a second set of
    transactions in the destination.
    """
    assert key() == key()


def test_key_is_stable_across_processes():
    """Hardcoded so a change to the derivation cannot pass silently.

    Changing what goes into the hash is a legitimate thing to do, but it
    changes every key, so records already posted under the old scheme can post
    again. This test is here to make that a conscious decision with a plan for
    the overlap window rather than a diff nobody noticed. If you are updating
    this value, bump KEY_VERSION in the same commit.
    """
    assert key() == (
        "v1-8641fc2d1e599ee1c3ee496410646eb018ad18753293176f94a101bccfc5f834"
    )


def test_keys_are_scoped_by_account_not_by_transaction_id_alone():
    """REGRESSION. Do not delete.

    Source systems frequently number transactions per account, so two accounts
    can each hold a transaction called "t1". With keys derived from the
    transaction id alone, the destination treats the second one as a repeat of
    the first, returns the original result, and the second transaction is
    never created. Money silently not moved, with a success in the log.
    """
    assert key(account_id="acct-1") != key(account_id="acct-2")


def test_business_date_is_part_of_the_key():
    """Sources that reuse ids across days must not collide onto one key."""
    assert key(business_date=date(2026, 3, 3)) != key(business_date=date(2026, 3, 4))


def test_source_system_is_part_of_the_key():
    assert key(source_system="src-a") != key(source_system="src-b")


def test_key_carries_its_version():
    assert key().startswith(f"{KEY_VERSION}-")


def test_delimiter_is_unambiguous():
    """('ab', 'c') and ('a', 'bc') must not hash to the same key.

    A delimiter that can appear inside the inputs makes the encoding
    ambiguous, and the resulting collision would be two different records
    sharing one idempotency key.
    """
    assert key(account_id="ab", transaction_id="c") != key(
        account_id="a", transaction_id="bc"
    )


@pytest.mark.parametrize("field", ["source_system", "account_id", "transaction_id"])
def test_empty_identity_fields_are_rejected(field: str):
    """An empty component would silently make unrelated records share a key."""
    with pytest.raises(ValueError, match=field):
        key(**{field: ""})


def test_checkpoint_id_is_also_scoped_by_account():
    """Same regression as the key, in the resume state.

    Scoped on transaction id alone, the checkpoint would mark acct-2's "t1" as
    already synced because acct-1's "t1" succeeded, and the record would be
    skipped on every future run.
    """
    assert checkpoint_id("acct-1", "t1") != checkpoint_id("acct-2", "t1")


def test_checkpoint_id_requires_both_parts():
    with pytest.raises(ValueError):
        checkpoint_id("", "t1")
    with pytest.raises(ValueError):
        checkpoint_id("acct-1", "")
