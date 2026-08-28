"""Dependency-light exact SQLite catalog SQL canonicalization."""

from __future__ import annotations

import hashlib
import re

_MAX_SCHEMA_SQL_LENGTH = 64 * 1024
_SQLITE_TOKEN_WHITESPACE = frozenset((" ", "\t", "\n", "\f", "\r"))


def _plain_schema_sql(value: object) -> str:
    if type(value) is not str:
        raise TypeError("SQLite schema SQL must be a plain string")
    if not value or len(value) > _MAX_SCHEMA_SQL_LENGTH:
        raise ValueError(
            "SQLite schema SQL must contain between 1 and "
            f"{_MAX_SCHEMA_SQL_LENGTH} characters"
        )
    return value


def canonicalize_backup_schema_sql(value: object) -> str:
    """Normalize only SQLite-token whitespace outside quotes and comments.

    Quoted content is copied byte-for-byte. A leading ``IF NOT EXISTS`` is removed
    because SQLite may omit it from ``sqlite_master.sql``. Comments are copied
    byte-for-byte, including the LF that terminates a ``--`` comment.
    """

    sql = _plain_schema_sql(value)
    output: list[str] = []
    plain_flags: list[bool] = []
    pending_whitespace = False

    def emit(text: str, *, plain: bool) -> None:
        output.extend(text)
        plain_flags.extend((plain,) * len(text))

    def flush_whitespace() -> None:
        nonlocal pending_whitespace
        if pending_whitespace and output:
            emit(" ", plain=True)
        pending_whitespace = False

    index = 0
    while index < len(sql):
        character = sql[index]
        if character in _SQLITE_TOKEN_WHITESPACE:
            pending_whitespace = bool(output)
            index += 1
            continue
        flush_whitespace()
        if sql.startswith("--", index):
            line_end = sql.find("\n", index + 2)
            if line_end < 0:
                emit(sql[index:], plain=False)
                index = len(sql)
            else:
                emit(sql[index : line_end + 1], plain=False)
                index = line_end + 1
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end < 0:
                raise ValueError("SQLite schema SQL contains an unterminated block comment")
            emit(sql[index : comment_end + 2], plain=False)
            index = comment_end + 2
            continue
        if character in {"'", '"', "`", "["}:
            quote_end = "]" if character == "[" else character
            quote_start = index
            index += 1
            while index < len(sql):
                if sql[index] != quote_end:
                    index += 1
                    continue
                if character != "[" and index + 1 < len(sql) and sql[index + 1] == quote_end:
                    index += 2
                    continue
                index += 1
                emit(sql[quote_start:index], plain=False)
                break
            else:
                raise ValueError("SQLite schema SQL contains an unterminated quoted region")
            continue
        emit(character, plain=True)
        index += 1

    while output and plain_flags[-1] and output[-1] in _SQLITE_TOKEN_WHITESPACE:
        output.pop()
        plain_flags.pop()
    if output and plain_flags[-1] and output[-1] == ";":
        output.pop()
        plain_flags.pop()
        while output and plain_flags[-1] and output[-1] in _SQLITE_TOKEN_WHITESPACE:
            output.pop()
            plain_flags.pop()
    canonical = "".join(output)
    if not canonical:
        raise ValueError("SQLite schema SQL has no canonical token content")
    canonical, _replacements = re.subn(
        r"\A(CREATE(?: UNIQUE)? (?:TABLE|INDEX|TRIGGER|VIEW)) IF NOT EXISTS\b",
        r"\1",
        canonical,
        count=1,
        flags=re.IGNORECASE,
    )
    return canonical


def backup_schema_ddl_sha256(value: object) -> str:
    """Hash one canonical explicit SQLite catalog definition."""

    return hashlib.sha256(canonicalize_backup_schema_sql(value).encode("utf-8")).hexdigest()


__all__ = ["backup_schema_ddl_sha256", "canonicalize_backup_schema_sql"]
