import sqlalchemy
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
