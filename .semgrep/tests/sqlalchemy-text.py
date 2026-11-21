from typing import Annotated

import sqlalchemy
from fastapi import Body, Query
from sqlalchemy import text
from sqlalchemy.sql import text as sql_text

TABLE = "users"


def positive(conn, user_id, name, col):
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(text(f"SELECT * FROM users WHERE id = {user_id}"))
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM users WHERE name = '%s'" % name))
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(text("SELECT {} FROM users".format(col)))
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM users WHERE name = '" + name + "'"))
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(sqlalchemy.text(f"DELETE FROM {TABLE} WHERE id = {user_id}"))
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(sql_text(f"SELECT * FROM users WHERE id = {user_id}"))


def negative(conn, user_id, name):
    # ok: sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id})
    # ok: sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM users WHERE name = :name").bindparams(name=name))
    # ok: sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM users " + "WHERE id = :id"), {"id": user_id})
    # ok: sqlalchemy-text-formatted-string
    conn.execute(sqlalchemy.text("SELECT 1"))
    # ok: sqlalchemy-text-formatted-string
    conn.execute(sql_text("SELECT * FROM users WHERE id = :id"), {"id": user_id})


# --- sqlalchemy-text-request-input (taint) ---


def bad_query_default(conn, q: str = Query(...)):
    # ruleid: sqlalchemy-text-request-input, sqlalchemy-text-formatted-string
    conn.execute(text(f"SELECT * FROM docs WHERE title LIKE '%{q}%'"))


def bad_annotated(conn, sort: Annotated[str, Query()] = "id"):
    # ruleid: sqlalchemy-text-request-input, sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM docs ORDER BY " + sort))


async def bad_body(conn, name: str = Body(...)):
    # ruleid: sqlalchemy-text-request-input, sqlalchemy-text-formatted-string
    conn.execute(text("SELECT * FROM docs WHERE name = '{}'".format(name)))


def good_bound(conn, q: str = Query(...)):
    # ok: sqlalchemy-text-request-input
    conn.execute(text("SELECT * FROM docs WHERE title LIKE :q"), {"q": f"%{q}%"})


def good_constant(conn, limit: int = Query(10)):
    table = "docs"
    # ok: sqlalchemy-text-request-input
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(text(f"SELECT * FROM {table} LIMIT {int(limit)}"))


def good_not_a_route(conn, q):
    # ok: sqlalchemy-text-request-input
    # ruleid: sqlalchemy-text-formatted-string
    conn.execute(text(f"SELECT * FROM {q}"))
