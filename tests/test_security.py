import pytest

from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_token,
)


def test_a_tampered_token_is_rejected():
    token = create_access_token("507f1f77bcf86cd799439011")

    with pytest.raises(AuthenticationError):
        decode_token(token + "tampered", expected_type="access")
