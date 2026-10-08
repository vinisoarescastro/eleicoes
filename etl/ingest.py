"""Carga dos arquivos "votacao_secao" do Portal de Dados Abertos do TSE no PostgreSQL.

Cada arquivo do TSE vira uma partição de tse.votacao_secao:
  AC..TO -> eleição estadual da UF (Governador, Senador, Deputados)
  BR     -> Presidente, com as seções de todas as UFs e do exterior
  ZZ     -> exterior (em 2026 publicado vazio; os votos do exterior vêm no BR)
Além deles:
  DETALHE -> "detalhe_votacao_secao" (aptos, comparecimento, abstenção, brancos e nulos) em tse.comparecimento_secao
  LOCAIS  -> "eleitorado_local_votacao" (endereço, bairro e coordenadas dos locais) em tse.local_votacao
  CANDIDATOS -> "consulta_cand" (nome de urna, partido, situação) em tse.candidato; dados pessoais são descartados
  MALHAS  -> malhas do IBGE (UFs, municípios, bairros) e correspondência TSE x IBGE. Não entra no ALL:
             roda quando pedido explicitamente ou quando o banco ainda não tem malhas.
Ao final, atualiza os bairros dos locais e os agregados do mapa quando algo mudou.

Antes da carga, aplica as migrações pendentes de ./migrations (registradas em tse.schema_migracao).

Conexão via variáveis padrão da libpq (PGHOST, PGUSER, PGPASSWORD, PGDATABASE).
Uso:
    python ingest.py --arquivos AC,BR,DETALHE
    python ingest.py --arquivos ALL --intervalo-min 60   # repete a cada 60 min
"""
import argparse
import csv
import io
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import requests
from psycopg import sql

import mapa

URL_BASE = os.getenv("TSE_URL_BASE", "https://cdn.tse.jus.br/estatistica/sead/odsele")
DIR_DOWNLOAD = Path(os.getenv("ETL_DIR_DOWNLOAD", "./dados"))
DIR_MIGRACOES = Path(__file__).parent / "migrations"
UFS = "AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO".split()
DETALHE, LOCAIS, MALHAS, CANDIDATOS = "DETALHE", "LOCAIS", "MALHAS", "CANDIDATOS"
ARQUIVOS = [*UFS, "BR", "ZZ", DETALHE, LOCAIS, CANDIDATOS, MALHAS]
ARQUIVOS_ALL = [a for a in ARQUIVOS if a != MALHAS]
# Impede duas cargas simultâneas (ex.: cron + execução manual)
LOCK_ETL = 2026_0001

COLUNAS = [
    "DT_GERACAO", "HH_GERACAO", "ANO_ELEICAO", "CD_TIPO_ELEICAO", "NM_TIPO_ELEICAO", "NR_TURNO",
    "CD_ELEICAO", "DS_ELEICAO", "DT_ELEICAO", "TP_ABRANGENCIA", "SG_UF", "SG_UE", "NM_UE",
    "CD_MUNICIPIO", "NM_MUNICIPIO", "NR_ZONA", "NR_SECAO", "CD_CARGO", "DS_CARGO", "NR_VOTAVEL",
    "NM_VOTAVEL", "QT_VOTOS", "NR_LOCAL_VOTACAO", "SQ_CANDIDATO", "NM_LOCAL_VOTACAO",
    "DS_LOCAL_VOTACAO_ENDERECO",
]

COLUNAS_DETALHE = [
    "DT_GERACAO", "HH_GERACAO", "ANO_ELEICAO", "CD_TIPO_ELEICAO", "NM_TIPO_ELEICAO", "NR_TURNO",
    "CD_ELEICAO", "DS_ELEICAO", "DT_ELEICAO", "TP_ABRANGENCIA", "SG_UF", "SG_UE", "NM_UE",
    "CD_MUNICIPIO", "NM_MUNICIPIO", "NR_ZONA", "NR_SECAO", "CD_CARGO", "DS_CARGO", "QT_APTOS",
    "QT_COMPARECIMENTO", "QT_ABSTENCOES", "QT_VOTOS_NOMINAIS", "QT_VOTOS_BRANCOS", "QT_VOTOS_NULOS",
    "QT_VOTOS_LEGENDA", "QT_VOTOS_ANULADOS_APU_SEP", "NR_LOCAL_VOTACAO", "NM_LOCAL_VOTACAO",
    "DS_LOCAL_VOTACAO_ENDERECO", "DT_RECEBIMENTO_BU_HOR_TSE", "DT_PRIM_TOT_PARCIAL_HOR_TSE",
    "DS_ORIGEM_VOTO", "ST_SECAO_INSTALADA", "ST_SECAO_ANULADA", "CD_MODELO_URNA", "DS_MODELO_URNA",
]
# Os zips de detalhe e de locais trazem um CSV por UF e o BRASIL (= todos). Carrega só o BRASIL.
MEMBRO_BRASIL = "_BRASIL.csv"

COLUNAS_LOCAIS = [
    "DT_GERACAO", "HH_GERACAO", "AA_ELEICAO", "DT_ELEICAO", "DS_ELEICAO", "NR_TURNO", "SG_UF",
    "CD_MUNICIPIO", "NM_MUNICIPIO", "NR_ZONA", "NR_SECAO", "CD_TIPO_SECAO_AGREGADA",
    "DS_TIPO_SECAO_AGREGADA", "NR_SECAO_PRINCIPAL", "NR_LOCAL_VOTACAO", "NM_LOCAL_VOTACAO",
    "CD_TIPO_LOCAL", "DS_TIPO_LOCAL", "DS_ENDERECO", "NM_BAIRRO", "NR_CEP", "NR_TELEFONE_LOCAL",
    "NR_LATITUDE", "NR_LONGITUDE", "CD_SITU_LOCAL_VOTACAO", "DS_SITU_LOCAL_VOTACAO", "CD_SITU_ZONA",
    "DS_SITU_ZONA", "CD_SITU_SECAO", "DS_SITU_SECAO", "CD_SITU_LOCALIDADE", "DS_SITU_LOCALIDADE",
    "CD_SITU_SECAO_ACESSIBILIDADE", "DS_SITU_SECAO_ACESSIBILIDADE", "QT_ELEITOR_SECAO",
    "QT_ELEITOR_ELEICAO_FEDERAL", "QT_ELEITOR_ELEICAO_ESTADUAL", "QT_ELEITOR_ELEICAO_MUNICIPAL",
    "NR_LOCAL_VOTACAO_ORIGINAL", "NM_LOCAL_VOTACAO_ORIGINAL", "DS_ENDERECO_LOCVT_ORIGINAL",
]

log = logging.getLogger("etl")

SQL_DIMENSOES = [
    """
    INSERT INTO tse.eleicao (cd_eleicao, ano_eleicao, nr_turno, cd_tipo_eleicao, nm_tipo_eleicao,
                             ds_eleicao, dt_eleicao, tp_abrangencia)
    SELECT DISTINCT ON (cd_eleicao::int)
           cd_eleicao::int, ano_eleicao::smallint, nr_turno::smallint, cd_tipo_eleicao::smallint,
           nm_tipo_eleicao, ds_eleicao, to_date(dt_eleicao, 'DD/MM/YYYY'), tp_abrangencia
      FROM stg
     ORDER BY cd_eleicao::int
    ON CONFLICT (cd_eleicao) DO UPDATE
       SET ano_eleicao = EXCLUDED.ano_eleicao, nr_turno = EXCLUDED.nr_turno,
           cd_tipo_eleicao = EXCLUDED.cd_tipo_eleicao, nm_tipo_eleicao = EXCLUDED.nm_tipo_eleicao,
           ds_eleicao = EXCLUDED.ds_eleicao, dt_eleicao = EXCLUDED.dt_eleicao,
           tp_abrangencia = EXCLUDED.tp_abrangencia
    """,
    """
    INSERT INTO tse.cargo (cd_cargo, ds_cargo)
    SELECT DISTINCT ON (cd_cargo::smallint) cd_cargo::smallint, ds_cargo
      FROM stg
     ORDER BY cd_cargo::smallint
    ON CONFLICT (cd_cargo) DO UPDATE SET ds_cargo = EXCLUDED.ds_cargo
    """,
    """
    INSERT INTO tse.municipio (cd_municipio, sg_uf, nm_municipio)
    SELECT DISTINCT ON (cd_municipio::int) cd_municipio::int, sg_uf, nm_municipio
      FROM stg
     ORDER BY cd_municipio::int
    ON CONFLICT (cd_municipio) DO UPDATE
       SET sg_uf = EXCLUDED.sg_uf, nm_municipio = EXCLUDED.nm_municipio
    """,
    """
    INSERT INTO tse.votavel (cd_eleicao, sg_ue, cd_cargo, nr_votavel, nm_votavel, sq_candidato)
    SELECT DISTINCT ON (cd_eleicao::int, sg_ue, cd_cargo::smallint, nr_votavel::int)
           cd_eleicao::int, sg_ue, cd_cargo::smallint, nr_votavel::int,
           nm_votavel, NULLIF(sq_candidato, '#NULO#')::bigint
      FROM stg
     ORDER BY cd_eleicao::int, sg_ue, cd_cargo::smallint, nr_votavel::int
    ON CONFLICT (cd_eleicao, sg_ue, cd_cargo, nr_votavel) DO UPDATE
       SET nm_votavel = EXCLUDED.nm_votavel, sq_candidato = EXCLUDED.sq_candidato
    """,
    """
    INSERT INTO tse.local_votacao (cd_municipio, nr_zona, nr_local_votacao, nm_local_votacao, ds_endereco)
    SELECT DISTINCT ON (cd_municipio::int, nr_zona::int, nr_local_votacao::int)
           cd_municipio::int, nr_zona::int, nr_local_votacao::int,
           NULLIF(nm_local_votacao, '#NULO#'), NULLIF(ds_local_votacao_endereco, '#NULO#')
      FROM stg
     WHERE nr_local_votacao IS NOT NULL AND nr_local_votacao <> '#NULO#'
     ORDER BY cd_municipio::int, nr_zona::int, nr_local_votacao::int
    ON CONFLICT (cd_municipio, nr_zona, nr_local_votacao) DO UPDATE
       SET nm_local_votacao = COALESCE(EXCLUDED.nm_local_votacao, tse.local_votacao.nm_local_votacao),
           ds_endereco = COALESCE(EXCLUDED.ds_endereco, tse.local_votacao.ds_endereco)
    """,
]

SQL_FATO = """
    INSERT INTO tse.votacao_secao (arquivo, cd_eleicao, sg_uf, sg_ue, cd_municipio, nr_zona, nr_secao,
                                   nr_local_votacao, cd_cargo, nr_votavel, qt_votos)
    SELECT %s, cd_eleicao::int, sg_uf, sg_ue, cd_municipio::int, nr_zona::int, nr_secao::int,
           NULLIF(nr_local_votacao, '#NULO#')::int, cd_cargo::smallint, nr_votavel::int, qt_votos::int
      FROM stg
"""

SQL_DETALHE = """
    INSERT INTO tse.comparecimento_secao (
        cd_eleicao, sg_uf, sg_ue, cd_municipio, nr_zona, nr_secao, cd_cargo,
        qt_aptos, qt_comparecimento, qt_abstencoes, qt_votos_nominais, qt_votos_brancos, qt_votos_nulos,
        qt_votos_legenda, qt_votos_anulados_apu_sep, nr_local_votacao, dt_recebimento_bu, dt_prim_tot_parcial,
        ds_origem_voto, st_secao_instalada, st_secao_anulada, cd_modelo_urna, ds_modelo_urna)
    SELECT cd_eleicao::int, sg_uf, sg_ue, cd_municipio::int, nr_zona::int, nr_secao::int, cd_cargo::smallint,
           NULLIF(qt_aptos, '#NULO#')::int, NULLIF(qt_comparecimento, '#NULO#')::int,
           NULLIF(qt_abstencoes, '#NULO#')::int, NULLIF(qt_votos_nominais, '#NULO#')::int,
           NULLIF(qt_votos_brancos, '#NULO#')::int, NULLIF(qt_votos_nulos, '#NULO#')::int,
           NULLIF(qt_votos_legenda, '#NULO#')::int, NULLIF(qt_votos_anulados_apu_sep, '#NULO#')::int,
           NULLIF(nr_local_votacao, '#NULO#')::int,
           to_timestamp(NULLIF(NULLIF(dt_recebimento_bu_hor_tse, '#NULO#'), ''), 'DD/MM/YYYY HH24:MI:SS')::timestamp,
           to_timestamp(NULLIF(NULLIF(dt_prim_tot_parcial_hor_tse, '#NULO#'), ''), 'DD/MM/YYYY HH24:MI:SS')::timestamp,
           NULLIF(ds_origem_voto, '#NULO#'),
           CASE st_secao_instalada WHEN 'Sim' THEN true WHEN 'Não' THEN false END,
           CASE st_secao_anulada WHEN 'Sim' THEN true WHEN 'Não' THEN false END,
           NULLIF(cd_modelo_urna, '#NULO#')::smallint, NULLIF(ds_modelo_urna, '#NULO#')
      FROM stg
"""


# Coordenadas vêm com vírgula decimal e sem zero à esquerda (",60574308"); -1 indica ausência.
# Só aceita pontos dentro do retângulo do território brasileiro.
SQL_LOCAIS = """
    INSERT INTO tse.local_votacao (cd_municipio, nr_zona, nr_local_votacao, nm_local_votacao, ds_endereco,
                                   nm_bairro_tse, nr_cep, geom)
    SELECT DISTINCT ON (cd_municipio, nr_zona, nr_local_votacao)
           cd_municipio, nr_zona, nr_local_votacao, nm_local_votacao, ds_endereco, nm_bairro, nr_cep,
           CASE WHEN lat BETWEEN -34 AND 6 AND lon BETWEEN -75 AND -28
                THEN ST_SetSRID(ST_MakePoint(lon, lat), 4326) END
      FROM (SELECT cd_municipio::int AS cd_municipio, nr_zona::int AS nr_zona,
                   nr_local_votacao::int AS nr_local_votacao, nr_turno::int AS nr_turno,
                   NULLIF(nm_local_votacao, '#NULO#') AS nm_local_votacao,
                   NULLIF(ds_endereco, '#NULO#') AS ds_endereco,
                   NULLIF(NULLIF(trim(nm_bairro), ''), '#NULO#') AS nm_bairro,
                   CASE WHEN nr_cep ~ '^[0-9]{8}$' THEN nr_cep END AS nr_cep,
                   CASE WHEN nr_latitude ~ '^-?[0-9]*,?[0-9]+$' THEN replace(nr_latitude, ',', '.')::float8 END AS lat,
                   CASE WHEN nr_longitude ~ '^-?[0-9]*,?[0-9]+$' THEN replace(nr_longitude, ',', '.')::float8 END AS lon
              FROM stg) s
     ORDER BY cd_municipio, nr_zona, nr_local_votacao, nr_turno DESC
    ON CONFLICT (cd_municipio, nr_zona, nr_local_votacao) DO UPDATE
       SET nm_local_votacao = coalesce(EXCLUDED.nm_local_votacao, tse.local_votacao.nm_local_votacao),
           ds_endereco      = coalesce(EXCLUDED.ds_endereco, tse.local_votacao.ds_endereco),
           nm_bairro_tse    = EXCLUDED.nm_bairro_tse,
           nr_cep           = EXCLUDED.nr_cep,
           geom             = EXCLUDED.geom
"""


# Únicas colunas lidas do consulta_cand. O arquivo também traz CPF, e-mail, título, data de nascimento e
# outros dados pessoais, que não são lidos nem gravados.
COLUNAS_CAND = [
    "DT_GERACAO", "HH_GERACAO", "SQ_CANDIDATO", "CD_ELEICAO", "NR_TURNO", "SG_UE", "CD_CARGO", "NR_CANDIDATO",
    "NM_URNA_CANDIDATO", "NR_PARTIDO", "SG_PARTIDO", "SG_FEDERACAO", "DS_SITUACAO_CANDIDATURA", "DS_SIT_TOT_TURNO",
]

SQL_CANDIDATOS = """
    INSERT INTO tse.candidato (sq_candidato, cd_eleicao, nr_turno, sg_ue, cd_cargo, nr_candidato, nm_urna,
                               nr_partido, sg_partido, sg_federacao, ds_situacao_candidatura, ds_sit_tot_turno)
    SELECT DISTINCT ON (sq_candidato::bigint)
           sq_candidato::bigint, cd_eleicao::int, nr_turno::smallint, sg_ue, cd_cargo::smallint, nr_candidato::int,
           NULLIF(NULLIF(nm_urna_candidato, '#NULO#'), '#NE'),
           CASE WHEN nr_partido ~ '^[0-9]+$' THEN nr_partido::smallint END,
           NULLIF(NULLIF(sg_partido, '#NULO#'), '#NE'),
           NULLIF(NULLIF(sg_federacao, '#NULO#'), '#NE'),
           NULLIF(NULLIF(ds_situacao_candidatura, '#NULO#'), '#NE'),
           NULLIF(NULLIF(ds_sit_tot_turno, '#NULO#'), '#NE')
      FROM stg_cand
     WHERE sq_candidato ~ '^[0-9]+$' AND nr_candidato ~ '^[0-9]+$'
     ORDER BY sq_candidato::bigint, nr_turno::int DESC
    ON CONFLICT (sq_candidato) DO UPDATE
       SET cd_eleicao = EXCLUDED.cd_eleicao, nr_turno = EXCLUDED.nr_turno, sg_ue = EXCLUDED.sg_ue,
           cd_cargo = EXCLUDED.cd_cargo, nr_candidato = EXCLUDED.nr_candidato, nm_urna = EXCLUDED.nm_urna,
           nr_partido = EXCLUDED.nr_partido, sg_partido = EXCLUDED.sg_partido, sg_federacao = EXCLUDED.sg_federacao,
           ds_situacao_candidatura = EXCLUDED.ds_situacao_candidatura, ds_sit_tot_turno = EXCLUDED.ds_sit_tot_turno
"""


def aplicar_migracoes(conn: psycopg.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS tse.schema_migracao (
                        versao text PRIMARY KEY, aplicada_em timestamptz NOT NULL DEFAULT now())""")
    aplicadas = {r[0] for r in conn.execute("SELECT versao FROM tse.schema_migracao")}
    for arq in sorted(DIR_MIGRACOES.glob("*.sql")):
        if arq.name in aplicadas:
            continue
        log.info("Aplicando migração %s", arq.name)
        with conn.transaction():
            conn.execute(arq.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO tse.schema_migracao (versao) VALUES (%s)", (arq.name,))


def validar_cabecalho(zip_path: Path, esperado: list[str], sufixo: str = ".csv") -> str:
    """Confere o layout do CSV e devolve o nome do membro dentro do zip."""
    with zipfile.ZipFile(zip_path) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith(sufixo.lower())]
        if len(csvs) != 1:
            raise ValueError(f"Esperado 1 CSV '*{sufixo}' no zip, encontrados {len(csvs)}: {csvs}")
        with zf.open(csvs[0]) as f:
            linha = f.readline()
    if linha.startswith(b"\xef\xbb\xbf"):
        raise ValueError("CSV em UTF-8 com BOM; layout diferente do esperado (Latin-1)")
    colunas = next(csv.reader([linha.decode("latin-1").strip()], delimiter=";"))
    if colunas != esperado:
        raise ValueError(f"Layout do CSV mudou. Recebido: {colunas}")
    return csvs[0]


def copiar_para_staging(cur: psycopg.Cursor, zip_path: Path, membro: str, colunas: list[str]) -> None:
    colunas_stg = sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c.lower())) for c in colunas)
    cur.execute(sql.SQL("CREATE TEMP TABLE stg ({}) ON COMMIT DROP").format(colunas_stg))
    with zipfile.ZipFile(zip_path) as zf, zf.open(membro) as f:
        with cur.copy("COPY stg FROM STDIN WITH (FORMAT csv, DELIMITER ';', HEADER true, ENCODING 'LATIN1')") as cp:
            while bloco := f.read(1 << 20):
                cp.write(bloco)


def info_remota(sess: requests.Session, url: str):
    r = sess.head(url, timeout=60, allow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return {
        "etag": r.headers.get("ETag"),
        "last_modified": r.headers.get("Last-Modified"),
        "tamanho": int(r.headers.get("Content-Length") or 0),
    }


def baixar(sess: requests.Session, url: str, destino: Path, tamanho_esperado: int) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(".part")
    with sess.get(url, stream=True, timeout=(30, 300)) as r:
        r.raise_for_status()
        with open(parcial, "wb") as f:
            for bloco in r.iter_content(chunk_size=1 << 20):
                f.write(bloco)
    if tamanho_esperado and parcial.stat().st_size != tamanho_esperado:
        raise IOError(f"Download incompleto: {parcial.stat().st_size} de {tamanho_esperado} bytes")
    parcial.replace(destino)


def carregar(conn: psycopg.Connection, arquivo: str, zip_path: Path) -> tuple[int, datetime | None]:
    """Recarrega a partição do arquivo em uma única transação. Arquivo vazio não altera o banco."""
    membro = validar_cabecalho(zip_path, COLUNAS)
    particao = sql.Identifier("tse", f"votacao_secao_{arquivo.lower()}")

    with conn.transaction(), conn.cursor() as cur:
        log.info("[%s] Copiando CSV para staging", arquivo)
        copiar_para_staging(cur, zip_path, membro, COLUNAS)

        cur.execute("SELECT count(*), count(*) FILTER (WHERE sg_uf <> %s) FROM stg", (arquivo,))
        linhas, fora_uf = cur.fetchone()
        if linhas == 0:
            return 0, None
        if arquivo != "BR" and fora_uf:
            raise ValueError(f"{fora_uf} linhas com SG_UF diferente de {arquivo}")

        cur.execute("SELECT max(to_timestamp(dt_geracao || ' ' || hh_geracao, 'DD/MM/YYYY HH24:MI:SS')::timestamp) FROM stg")
        dt_geracao = cur.fetchone()[0]

        log.info("[%s] Atualizando dimensões", arquivo)
        for comando in SQL_DIMENSOES:
            cur.execute(comando)

        log.info("[%s] Recarregando partição (%s linhas)", arquivo, linhas)
        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} PARTITION OF tse.votacao_secao FOR VALUES IN ({})")
            .format(particao, sql.Literal(arquivo))
        )
        cur.execute(sql.SQL("TRUNCATE {}").format(particao))
        cur.execute(SQL_FATO, (arquivo,))
        cur.execute(sql.SQL("ANALYZE {}").format(particao))

    return linhas, dt_geracao


def carregar_detalhe(conn: psycopg.Connection, arquivo: str, zip_path: Path) -> tuple[int, datetime | None]:
    """Recarrega tse.comparecimento_secao inteira em uma única transação."""
    membro = validar_cabecalho(zip_path, COLUNAS_DETALHE, MEMBRO_BRASIL)

    with conn.transaction(), conn.cursor() as cur:
        log.info("[%s] Copiando %s para staging", arquivo, membro)
        copiar_para_staging(cur, zip_path, membro, COLUNAS_DETALHE)

        cur.execute("SELECT count(*) FROM stg")
        linhas = cur.fetchone()[0]
        if linhas == 0:
            return 0, None
        cur.execute("SELECT max(to_timestamp(dt_geracao || ' ' || hh_geracao, 'DD/MM/YYYY HH24:MI:SS')::timestamp) FROM stg")
        dt_geracao = cur.fetchone()[0]

        # Seções sem voto (ex.: não instaladas no exterior) podem trazer municípios ausentes dos arquivos de votos
        cur.execute("""
            INSERT INTO tse.municipio (cd_municipio, sg_uf, nm_municipio)
            SELECT DISTINCT ON (cd_municipio::int) cd_municipio::int, sg_uf, nm_municipio
              FROM stg
             ORDER BY cd_municipio::int
            ON CONFLICT (cd_municipio) DO NOTHING
        """)

        log.info("[%s] Recarregando tse.comparecimento_secao (%s linhas)", arquivo, linhas)
        cur.execute("TRUNCATE tse.comparecimento_secao")
        cur.execute(SQL_DETALHE)
        cur.execute("ANALYZE tse.comparecimento_secao")

    return linhas, dt_geracao


def carregar_locais(conn: psycopg.Connection, arquivo: str, zip_path: Path) -> tuple[int, datetime | None]:
    """Atualiza endereço, bairro (TSE) e coordenadas de tse.local_votacao. Não remove locais existentes."""
    membro = validar_cabecalho(zip_path, COLUNAS_LOCAIS, MEMBRO_BRASIL)

    with conn.transaction(), conn.cursor() as cur:
        log.info("[%s] Copiando %s para staging", arquivo, membro)
        copiar_para_staging(cur, zip_path, membro, COLUNAS_LOCAIS)

        cur.execute("SELECT count(*) FROM stg")
        linhas = cur.fetchone()[0]
        if linhas == 0:
            return 0, None
        cur.execute("SELECT max(to_timestamp(dt_geracao || ' ' || hh_geracao, 'DD/MM/YYYY HH24:MI:SS')::timestamp) FROM stg")
        dt_geracao = cur.fetchone()[0]

        log.info("[%s] Atualizando locais de votação (%s seções no arquivo)", arquivo, linhas)
        cur.execute(SQL_LOCAIS)
        cur.execute("ANALYZE tse.local_votacao")

    return linhas, dt_geracao


def carregar_candidatos(conn: psycopg.Connection, arquivo: str, zip_path: Path) -> tuple[int, datetime | None]:
    """Lê do CSV apenas COLUNAS_CAND (descarta dados pessoais na leitura) e atualiza tse.candidato."""
    with zipfile.ZipFile(zip_path) as zf:
        membros = [n for n in zf.namelist() if n.upper().endswith(MEMBRO_BRASIL.upper())]
        if len(membros) != 1:
            raise ValueError(f"Esperado 1 CSV '*{MEMBRO_BRASIL}' no zip, encontrados {len(membros)}")
        with zf.open(membros[0]) as f:
            leitor = csv.DictReader(io.TextIOWrapper(f, encoding="latin-1", newline=""), delimiter=";")
            faltando = set(COLUNAS_CAND) - set(leitor.fieldnames or [])
            if faltando:
                raise ValueError(f"Layout do CSV de candidatos mudou. Colunas ausentes: {sorted(faltando)}")
            linhas = [tuple(r[c] for c in COLUNAS_CAND) for r in leitor]
    if not linhas:
        return 0, None
    dt_geracao = datetime.strptime(f"{linhas[0][0]} {linhas[0][1]}", "%d/%m/%Y %H:%M:%S")

    with conn.transaction(), conn.cursor() as cur:
        colunas = sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c.lower())) for c in COLUNAS_CAND)
        cur.execute(sql.SQL("CREATE TEMP TABLE stg_cand ({}) ON COMMIT DROP").format(colunas))
        with cur.copy("COPY stg_cand FROM STDIN") as cp:
            for linha in linhas:
                cp.write_row(linha)
        log.info("[%s] Atualizando candidatos (%s linhas)", arquivo, len(linhas))
        cur.execute(SQL_CANDIDATOS)
    return len(linhas), dt_geracao


def registrar(conn, ano, arquivo, url, **campos) -> None:
    colunas = ["ano_eleicao", "arquivo", "url", *campos.keys()]
    valores = [ano, arquivo, url, *campos.values()]
    atualizar = sql.SQL(", ").join(
        sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in ["url", *campos.keys()]
    )
    with conn.transaction():
        conn.execute(
            sql.SQL("INSERT INTO tse.carga_controle ({}) VALUES ({}) ON CONFLICT (ano_eleicao, arquivo) DO UPDATE SET {}")
            .format(sql.SQL(", ").join(map(sql.Identifier, colunas)), sql.SQL(", ").join(sql.Placeholder() * len(valores)), atualizar),
            valores,
        )


def processar(conn, sess, ano: int, arquivo: str, forcar: bool) -> tuple[bool, bool]:
    """Baixa e carrega um arquivo. Retorna (sucesso, houve_carga)."""
    if arquivo == DETALHE:
        url, carregador = f"{URL_BASE}/detalhe_votacao_secao/detalhe_votacao_secao_{ano}.zip", carregar_detalhe
        zip_path = DIR_DOWNLOAD / f"detalhe_votacao_secao_{ano}.zip"
    elif arquivo == CANDIDATOS:
        url, carregador = f"{URL_BASE}/consulta_cand/consulta_cand_{ano}.zip", carregar_candidatos
        zip_path = DIR_DOWNLOAD / f"consulta_cand_{ano}.zip"
    elif arquivo == LOCAIS:
        url, carregador = f"{URL_BASE}/eleitorado_locais_votacao/eleitorado_local_votacao_{ano}.zip", carregar_locais
        zip_path = DIR_DOWNLOAD / f"eleitorado_local_votacao_{ano}.zip"
    else:
        url, carregador = f"{URL_BASE}/votacao_secao/votacao_secao_{ano}_{arquivo}.zip", carregar
        zip_path = DIR_DOWNLOAD / f"votacao_secao_{ano}_{arquivo}.zip"
    remoto = info_remota(sess, url)
    if remoto is None:
        log.warning("[%s] Arquivo ainda não publicado (404)", arquivo)
        return True, False

    anterior = conn.execute(
        "SELECT etag, last_modified FROM tse.carga_controle WHERE ano_eleicao = %s AND arquivo = %s AND status IN ('OK', 'VAZIO')",
        (ano, arquivo),
    ).fetchone()
    if not forcar and anterior and anterior == (remoto["etag"], remoto["last_modified"]):
        log.info("[%s] Sem alterações desde a última carga", arquivo)
        return True, False

    registrar(conn, ano, arquivo, url, status="EM_ANDAMENTO", mensagem=None,
              iniciado_em=datetime.now(timezone.utc), concluido_em=None)
    try:
        log.info("[%s] Baixando %.1f MB", arquivo, remoto["tamanho"] / 1e6)
        baixar(sess, url, zip_path, remoto["tamanho"])
        linhas, dt_geracao = carregador(conn, arquivo, zip_path)
    except Exception as e:  # a transação de carga já foi desfeita; registra a falha e segue para o próximo arquivo
        log.exception("[%s] Falha na carga", arquivo)
        registrar(conn, ano, arquivo, url, status="ERRO", mensagem=str(e)[:1000], concluido_em=datetime.now(timezone.utc))
        return False, False
    finally:
        zip_path.unlink(missing_ok=True)

    status = "OK" if linhas else "VAZIO"
    registrar(
        conn, ano, arquivo, url, status=status, mensagem=None, linhas=linhas, dt_geracao=dt_geracao,
        etag=remoto["etag"], last_modified=remoto["last_modified"], concluido_em=datetime.now(timezone.utc),
    )
    log.info("[%s] Concluído (%s): %s linhas, gerado pelo TSE em %s", arquivo, status, linhas, dt_geracao)
    return True, bool(linhas)


def processar_malhas(conn, sess, ano: int) -> bool:
    registrar(conn, ano, MALHAS, mapa.URL_IBGE_MALHAS, status="EM_ANDAMENTO", mensagem=None,
              iniciado_em=datetime.now(timezone.utc), concluido_em=None)
    try:
        DIR_DOWNLOAD.mkdir(parents=True, exist_ok=True)
        linhas = mapa.carregar_malhas(conn, sess, DIR_DOWNLOAD)
    except Exception as e:
        log.exception("[%s] Falha na carga", MALHAS)
        registrar(conn, ano, MALHAS, mapa.URL_IBGE_MALHAS, status="ERRO", mensagem=str(e)[:1000],
                  concluido_em=datetime.now(timezone.utc))
        return False
    registrar(conn, ano, MALHAS, mapa.URL_IBGE_MALHAS, status="OK", mensagem=None, linhas=linhas,
              concluido_em=datetime.now(timezone.utc))
    log.info("[%s] Concluído: %s geometrias", MALHAS, linhas)
    return True


def parse_arquivos(valor: str) -> list[str]:
    if valor.strip().upper() == "ALL":
        return ARQUIVOS_ALL
    arquivos = [a.strip().upper() for a in valor.split(",") if a.strip()]
    invalidos = [a for a in arquivos if a not in ARQUIVOS]
    if invalidos:
        raise SystemExit(f"Arquivo(s) inválido(s): {invalidos}. Use UFs, BR, ZZ, DETALHE, LOCAIS, CANDIDATOS, MALHAS ou ALL.")
    return arquivos


def executar(args) -> bool:
    ok = True
    # autocommit: cada bloco conn.transaction() é uma transação real (e não um savepoint)
    with psycopg.connect("", autocommit=True) as conn, requests.Session() as sess:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ETL,)).fetchone()[0]:
            log.warning("Outra carga está em andamento; encerrando sem fazer nada")
            return True
        aplicar_migracoes(conn)
        sess.headers["User-Agent"] = "eleicoes-etl/1.0"

        arquivos = parse_arquivos(args.arquivos)
        votos_alterados = localizacao_alterada = False
        for arquivo in (a for a in arquivos if a != MALHAS):
            sucesso, carregado = processar(conn, sess, args.ano, arquivo, args.forcar)
            ok = sucesso and ok
            votos_alterados |= carregado and arquivo not in (DETALHE, LOCAIS, CANDIDATOS)
            localizacao_alterada |= carregado and arquivo == LOCAIS

        sem_malhas = conn.execute("SELECT NOT EXISTS (SELECT 1 FROM geo.uf)").fetchone()[0]
        if MALHAS in arquivos or sem_malhas:
            sucesso = processar_malhas(conn, sess, args.ano)
            ok = sucesso and ok
            localizacao_alterada |= sucesso

        try:
            sem_bairro = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM tse.local_votacao WHERE bairro_fonte IS NULL AND nm_bairro_tse IS NOT NULL)"
            ).fetchone()[0]
            if localizacao_alterada or sem_bairro:
                mapa.atualizar_bairros(conn)
            mapa.atualizar_agregados(conn, forcar=votos_alterados)
        except Exception:
            log.exception("Falha ao atualizar bairros/agregados do mapa")
            ok = False
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ano", type=int, default=int(os.getenv("ETL_ANO", "2026")))
    p.add_argument("--arquivos", default=os.getenv("ETL_ARQUIVOS", "AC,BR"),
                   help="UFs, BR (Presidente), ZZ, DETALHE, LOCAIS, CANDIDATOS, MALHAS, separados por vírgula, ou ALL")
    p.add_argument("--forcar", action="store_true", help="Recarrega mesmo sem alteração no arquivo remoto")
    p.add_argument("--intervalo-min", type=int, default=0, help="Se > 0, repete a carga a cada N minutos")
    args = p.parse_args()

    if args.intervalo_min <= 0:
        sys.exit(0 if executar(args) else 1)
    while True:
        try:
            executar(args)
        except Exception:  # falha de rede/banco não derruba o modo contínuo
            log.exception("Falha no ciclo de carga")
        log.info("Próxima verificação em %s min", args.intervalo_min)
        time.sleep(args.intervalo_min * 60)


if __name__ == "__main__":
    main()
