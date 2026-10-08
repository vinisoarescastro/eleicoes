"""Pool de conexões somente leitura (PGHOST/PGUSER/PGPASSWORD/PGDATABASE; PGOPTIONS define timeout e read-only)."""
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    conninfo="",
    min_size=1,
    max_size=int(os.getenv("API_POOL_MAX", "10")),
    open=False,
    kwargs={"row_factory": dict_row},
)


def consultar(comando: str, params: dict | tuple = ()) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(comando, params).fetchall()


def consultar_valor(comando: str, params: dict | tuple = ()):
    """Retorna a primeira coluna da primeira linha (ex.: JSON montado no próprio SQL)."""
    with pool.connection() as conn:
        linha = conn.execute(comando, params).fetchone()
    return next(iter(linha.values())) if linha else None
