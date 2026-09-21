"""Small form validation helpers."""

import re

_USERNAME = re.compile(r"[A-Za-z0-9_.-]{3,80}")


class FormError(ValueError):
    """A safe validation message suitable for display."""


def validate_username(value: str) -> str:
    username = value.strip()
    if _USERNAME.fullmatch(username) is None:
        raise FormError("账号需为 3-80 位字母、数字、点、下划线或连字符")
    return username


def validate_password(value: str) -> str:
    if len(value) < 12:
        raise FormError("密码至少需要 12 个字符")
    if len(value) > 512:
        raise FormError("密码过长")
    return value
