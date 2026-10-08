"""Cargas de apoio ao mapa: malhas do IBGE, correspondência TSE x IBGE, bairros e agregados de votos."""
import io
import json
import logging
import os
import zipfile

import psycopg
import requests
import shapefile

log = logging.getLogger("etl")

URL_IBGE_MALHAS = "https://servicodados.ibge.gov.br/api/v3/malhas"
URL_BAIRROS = os.getenv(
    "IBGE_URL_BAIRROS",
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
    "malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/bairros/shp/BR/BR_bairros_CD2022.zip",
)
# Configuração de municípios do painel de resultados: traz o código TSE (cd) e o código IBGE (cdi)
URL_CONFIG_MUN = os.getenv(
    "TSE_URL_CONFIG_MUN", "https://resultados.tse.jus.br/oficial/ele2026/6257/config/mun-e006257-cm.json"
)
UF_IBGE = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17, "MA": 21, "PI": 22,
    "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29, "MG": 31, "ES": 32,
    "RJ": 33, "SP": 35, "PR": 41, "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}
# Distância máxima (km) entre o local e o próprio município para a coordenada ser considerada válida
LIMITE_KM = float(os.getenv("ETL_LIMITE_COORD_KM", "2"))
# Polígonos válidos e sempre MultiPolygon
GEOM = "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), 3))"


def _malha(sess: requests.Session, caminho: str, **params) -> list[dict]:
    r = sess.get(f"{URL_IBGE_MALHAS}/{caminho}", params={"formato": "application/vnd.geo+json", **params}, timeout=180)
    r.raise_for_status()
    return r.json()["features"]


def _ler_bairros(sess: requests.Session, destino) -> list[tuple]:
    """Lê o shapefile de bairros do IBGE. O .cpg declara UTF-8, mas o DBF está em Latin-1."""
    with sess.get(URL_BAIRROS, stream=True, timeout=(30, 600)) as r:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloco in r.iter_content(chunk_size=1 << 20):
                f.write(bloco)
    with zipfile.ZipFile(destino) as zf:
        partes = {n.rsplit(".", 1)[-1].lower(): io.BytesIO(zf.read(n)) for n in zf.namelist()}
    leitor = shapefile.Reader(shp=partes["shp"], shx=partes["shx"], dbf=partes["dbf"], encoding="latin-1")
    campos = [c[0] for c in leitor.fields[1:]]
    if not {"CD_BAIRRO", "NM_BAIRRO", "CD_MUN"} <= set(campos):
        raise ValueError(f"Layout do shapefile de bairros mudou: {campos}")
    bairros = []
    for sr in leitor.iterShapeRecords():
        reg = sr.record.as_dict()
        bairros.append((int(reg["CD_BAIRRO"]), int(reg["CD_MUN"]), reg["NM_BAIRRO"].strip(),
                        _json(sr.shape.__geo_interface__)))
    return bairros


def _json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def carregar_malhas(conn: psycopg.Connection, sess: requests.Session, dir_download) -> int:
    """Recarrega geo.uf, geo.municipio, geo.bairro e tse.municipio.cd_ibge em uma única transação."""
    log.info("[MALHAS] Baixando malha das UFs")
    sigla = {v: k for k, v in UF_IBGE.items()}
    ufs = [(sigla[int(f["properties"]["codarea"])], int(f["properties"]["codarea"]), _json(f["geometry"]))
           for f in _malha(sess, "paises/BR", intrarregiao="UF", qualidade="minima")]

    municipios = []
    for uf, cod in UF_IBGE.items():
        log.info("[MALHAS] Baixando malha municipal de %s", uf)
        municipios += [(int(f["properties"]["codarea"]), uf, _json(f["geometry"]))
                       for f in _malha(sess, f"estados/{cod}", intrarregiao="municipio", qualidade="intermediaria")]

    log.info("[MALHAS] Baixando correspondência de municípios TSE x IBGE")
    r = sess.get(URL_CONFIG_MUN, timeout=120)
    r.raise_for_status()
    correspondencia = [(int(mu["cd"]), uf["cd"].upper(), mu["nm"], int(mu["cdi"]))
                       for uf in r.json()["abr"] for mu in uf["mu"] if mu.get("cdi")]

    log.info("[MALHAS] Baixando bairros do Censo 2022")
    zip_path = dir_download / "bairros_ibge.zip"
    try:
        bairros = _ler_bairros(sess, zip_path)
    finally:
        zip_path.unlink(missing_ok=True)

    log.info("[MALHAS] Gravando %s UFs, %s municípios, %s bairros e %s correspondências",
             len(ufs), len(municipios), len(bairros), len(correspondencia))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("TRUNCATE geo.uf, geo.municipio, geo.bairro")
        cur.executemany(f"INSERT INTO geo.uf (sg_uf, cd_ibge, geom) VALUES (%s, %s, {GEOM})", ufs)
        cur.executemany(f"INSERT INTO geo.municipio (cd_ibge, sg_uf, geom) VALUES (%s, %s, {GEOM})", municipios)
        cur.executemany(
            f"INSERT INTO geo.bairro (cd_bairro, cd_mun_ibge, nm_bairro, geom) VALUES (%s, %s, %s, {GEOM})", bairros
        )
        cur.executemany(
            """INSERT INTO tse.municipio (cd_municipio, sg_uf, nm_municipio, cd_ibge) VALUES (%s, %s, %s, %s)
               ON CONFLICT (cd_municipio) DO UPDATE SET cd_ibge = EXCLUDED.cd_ibge""",
            correspondencia,
        )
        cur.execute("ANALYZE geo.uf; ANALYZE geo.municipio; ANALYZE geo.bairro")
    return len(municipios) + len(bairros)


def atualizar_bairros(conn: psycopg.Connection) -> None:
    """Valida as coordenadas e define o bairro de cada local: polígono oficial do IBGE que contém o ponto;
    senão, o nome informado pelo TSE."""
    log.info("Validando coordenadas e atribuindo bairros aos locais de votação")
    with conn.transaction():
        # Coordenada válida: dentro do próprio município ou a até LIMITE_KM da borda (tolerância da malha simplificada).
        # Município sem malha (ex.: criado após o Censo 2022) mantém a coordenada.
        conn.execute("""
            UPDATE tse.local_votacao l
               SET coord_valida = l.geom IS NOT NULL AND (
                       g.geom IS NULL OR ST_DWithin(g.geom::geography, l.geom::geography, %s * 1000))
              FROM tse.municipio m
              LEFT JOIN geo.municipio g ON g.cd_ibge = m.cd_ibge
             WHERE m.cd_municipio = l.cd_municipio
        """, (LIMITE_KM,))
        conn.execute("""
            UPDATE tse.local_votacao l
               SET cd_bairro_ibge = b.cd_bairro,
                   bairro_fonte   = CASE WHEN b.cd_bairro IS NOT NULL THEN 'IBGE'
                                         WHEN l.nm_bairro_tse IS NOT NULL THEN 'TSE' END,
                   bairro_nome    = coalesce(b.nm_bairro, initcap(l.nm_bairro_tse)),
                   bairro_chave   = CASE WHEN b.cd_bairro IS NOT NULL THEN 'I' || b.cd_bairro
                                         WHEN l.nm_bairro_tse IS NOT NULL
                                         THEN 'T' || upper(unaccent(regexp_replace(trim(l.nm_bairro_tse), '\\s+', ' ', 'g')))
                                    END
              FROM tse.local_votacao l2
              LEFT JOIN tse.municipio m ON m.cd_municipio = l2.cd_municipio
              LEFT JOIN LATERAL (
                    SELECT gb.cd_bairro, gb.nm_bairro
                      FROM geo.bairro gb
                     WHERE gb.cd_mun_ibge = m.cd_ibge AND l2.coord_valida AND ST_Covers(gb.geom, l2.geom)
                     LIMIT 1
              ) b ON true
             WHERE l.cd_municipio = l2.cd_municipio AND l.nr_zona = l2.nr_zona
               AND l.nr_local_votacao = l2.nr_local_votacao
        """)


def atualizar_agregados(conn: psycopg.Connection, forcar: bool) -> None:
    """Atualiza as views materializadas do mapa (sem bloquear leitura quando já populadas)."""
    for mv in ("votos_local", "votos_municipio", "votos_uf"):
        populada = conn.execute(
            "SELECT ispopulated FROM pg_matviews WHERE schemaname = 'tse' AND matviewname = %s", (mv,)
        ).fetchone()[0]
        if populada and not forcar:
            continue
        log.info("Atualizando agregado tse.%s", mv)
        modo = "CONCURRENTLY " if populada else ""
        conn.execute(f"REFRESH MATERIALIZED VIEW {modo}tse.{mv}")
        conn.execute(f"ANALYZE tse.{mv}")
