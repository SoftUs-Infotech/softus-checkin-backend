import pytest

from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_token,
    hash_password,
    hash_token,
    tokens_match,
    verify_password,
)


def test_password_round_trip():
    hashed = hash_password("S0me-Str0ng-Pass!")

    assert hashed != "S0me-Str0ng-Pass!"
    assert verify_password("S0me-Str0ng-Pass!", hashed)
    assert not verify_password("wrong-password", hashed)


def test_same_password_hashes_differently_each_time():
    assert hash_password("repeat-me") != hash_password("repeat-me")


def test_long_passphrases_are_not_truncated_at_bcrypts_72_byte_limit():
    base = "x" * 80
    hashed = hash_password(base + "-ending-A")

    assert verify_password(base + "-ending-A", hashed)
    assert not verify_password(base + "-ending-B", hashed)


def test_verify_password_rejects_a_malformed_hash():
    assert not verify_password("anything", "not-a-bcrypt-hash")
    assert not verify_password("anything", "")


def test_access_token_carries_subject_and_role():
    token = create_access_token("507f1f77bcf86cd799439011", role="ADMIN")
    payload = decode_token(token, expected_type="access")

    assert payload["sub"] == "507f1f77bcf86cd799439011"
    assert payload["role"] == "ADMIN"
    assert payload["type"] == "access"


def test_refresh_token_reuses_a_family_id_across_rotations():
    first, family = create_refresh_token("507f1f77bcf86cd799439011")
    second, same_family = create_refresh_token(
        "507f1f77bcf86cd799439011", family_id=family
    )

    assert family == same_family
    assert first != second
    assert decode_token(second, expected_type="refresh")["fam"] == family


def test_an_access_token_is_rejected_where_a_refresh_token_is_expected():
    token = create_access_token("507f1f77bcf86cd799439011")

    with pytest.raises(AuthenticationError) as exc:
        decode_token(token, expected_type="refresh")

    assert exc.value.code == "INVALID_TOKEN_TYPE"


def test_a_tampered_token_is_rejected():
    token = create_access_token("507f1f77bcf86cd799439011")

    with pytest.raises(AuthenticationError):
        decode_token(token + "tampered", expected_type="access")


def test_opaque_tokens_are_compared_by_digest():
    token = generate_token()
    stored = hash_token(token)

    assert stored != token
    assert tokens_match(token, stored)
    assert not tokens_match(generate_token(), stored)
