"""Endpoints públicos do mapa: GeoJSON por nível (UF, município, bairro, local), resumo do recorte e busca.

Indicadores de cada área (sobre votos válidos = nominais + legenda):
- `top`: os 3 mais votados (exclui brancos/nulos e, para Deputados, votos de legenda), com nome de urna e partido;
- `venc_*` e `margem`: vencedor e vantagem em pontos percentuais sobre o 2º colocado;
- `a_*` / `b_*`: votos e % dos candidatos escolhidos (`numero`/`ue` e `numero2`/`ue2`), para os modos
  "% de um candidato" e "Comparar".
Nomes: nome de urna (tse.candidato); sem cadastro, o nome do arquivo de votos.
"""
import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from db import consultar, consultar_valor

router = APIRouter(prefix="/mapa", tags=["mapa"])

CACHE = "public, max-age=300"
UF_REGEX = r"^[A-Za-z]{2}$"
UE_REGEX = r"^[A-Za-z]{2,5}$"
CARGOS_DEPUTADO = (6, 7, 8)


def _padrao_json(valor):
    """Decimal vira número; datas e demais tipos, texto."""
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    return str(valor)


def _json(conteudo) -> str:
    return json.dumps(conteudo, default=_padrao_json, ensure_ascii=False)


def _resposta(conteudo, tipo="application/json") -> Response:
    corpo = conteudo if isinstance(conteudo, str) else _json(conteudo)
    return Response(content=corpo, media_type=tipo, headers={"Cache-Control": CACHE})


def _geojson(conteudo) -> Response:
    return _resposta(conteudo, "application/geo+json")


# Nome de urna e partido de cada votável do cargo
NOMES = """
    nomes AS (
        SELECT vt.sg_ue, vt.nr_votavel, coalesce(c.nm_urna, vt.nm_votavel) AS nome, c.sg_partido,
               c.ds_sit_tot_turno AS situacao
          FROM tse.votavel vt
          LEFT JOIN tse.candidato c ON c.sq_candidato = vt.sq_candidato
         WHERE vt.cd_eleicao = %(ele)s AND vt.cd_cargo = %(cargo)s
    )"""

FILTRO_NOMINAL = f"nr_votavel NOT IN (95, 96) AND (%(cargo)s NOT IN {CARGOS_DEPUTADO} OR nr_votavel >= 100)"


def _indicadores(fonte: str) -> str:
    """CTEs de indicadores por área. `fonte` deve expor area, nr_votavel, sg_ue, qt_votos."""
    return f"""{NOMES},
        v AS ({fonte}),
        tot AS (
            SELECT area,
                   sum(qt_votos) FILTER (WHERE nr_votavel NOT IN (95, 96)) AS validos,
                   sum(qt_votos) FILTER (WHERE nr_votavel = 95) AS brancos,
                   sum(qt_votos) FILTER (WHERE nr_votavel = 96) AS nulos,
                   sum(qt_votos) AS total
              FROM v GROUP BY area
        ),
        rk AS (
            SELECT area, nr_votavel, sg_ue, qt_votos,
                   row_number() OVER (PARTITION BY area ORDER BY qt_votos DESC, nr_votavel) AS pos
              FROM v WHERE {FILTRO_NOMINAL}
        ),
        top AS (
            SELECT rk.area,
                   json_agg(json_build_object(
                       'nr', rk.nr_votavel, 'ue', rk.sg_ue, 'nome', n.nome, 'partido', n.sg_partido,
                       'votos', rk.qt_votos, 'pct', round(100.0 * rk.qt_votos / NULLIF(t.validos, 0), 2)
                   ) ORDER BY rk.pos) AS top,
                   max(rk.qt_votos) FILTER (WHERE rk.pos = 1) AS v1,
                   max(rk.qt_votos) FILTER (WHERE rk.pos = 2) AS v2
              FROM rk
              JOIN tot t ON t.area = rk.area
              LEFT JOIN nomes n ON n.sg_ue = rk.sg_ue AND n.nr_votavel = rk.nr_votavel
             WHERE rk.pos <= 3
             GROUP BY rk.area
        ),
        ca AS (
            SELECT area, sum(qt_votos) AS q FROM v
             WHERE nr_votavel = %(num)s AND (%(ue)s::text IS NULL OR sg_ue = %(ue)s) GROUP BY area
        ),
        cb AS (
            SELECT area, sum(qt_votos) AS q FROM v
             WHERE nr_votavel = %(num2)s AND (%(ue2)s::text IS NULL OR sg_ue = %(ue2)s) GROUP BY area
        ),
        ind AS (
            SELECT t.area, t.validos, t.brancos, t.nulos, t.total, tp.top,
                   (tp.top -> 0 ->> 'nr')::int AS venc_nr, tp.top -> 0 ->> 'ue' AS venc_ue,
                   tp.top -> 0 ->> 'nome' AS venc_nome, tp.top -> 0 ->> 'partido' AS venc_partido,
                   round(100.0 * tp.v1 / NULLIF(t.validos, 0), 2) AS venc_pct,
                   round(100.0 * (tp.v1 - coalesce(tp.v2, 0)) / NULLIF(t.validos, 0), 2) AS margem,
                   ca.q AS a_votos, round(100.0 * ca.q / NULLIF(t.validos, 0), 2) AS a_pct,
                   cb.q AS b_votos, round(100.0 * cb.q / NULLIF(t.validos, 0), 2) AS b_pct
              FROM tot t
              LEFT JOIN top tp ON tp.area = t.area
              LEFT JOIN ca ON ca.area = t.area
              LEFT JOIN cb ON cb.area = t.area
        )"""


PROPS_INDICADOR = """
    'validos', i.validos, 'brancos', i.brancos, 'nulos', i.nulos, 'total', i.total, 'top', i.top,
    'venc_nr', i.venc_nr, 'venc_ue', i.venc_ue, 'venc_nome', i.venc_nome, 'venc_partido', i.venc_partido,
    'venc_pct', i.venc_pct, 'margem', i.margem,
    'a_votos', i.a_votos, 'a_pct', i.a_pct, 'b_votos', i.b_votos, 'b_pct', i.b_pct"""


def _feature_collection(geom: str, props: str, origem: str) -> str:
    return f"""
        SELECT json_build_object('type', 'FeatureCollection', 'features', coalesce(json_agg(
                   json_build_object('type', 'Feature', 'geometry', {geom}::json,
                                     'properties', json_build_object({props}))), '[]'::json))::text
          FROM {origem}"""


class Filtros:
    """Parâmetros comuns dos endpoints GeoJSON."""

    def __init__(self, cd_eleicao: int, cargo: int, numero: int | None = None,
                 ue: str | None = Query(default=None, pattern=UE_REGEX), numero2: int | None = None,
                 ue2: str | None = Query(default=None, pattern=UE_REGEX)):
        self.params = {"ele": cd_eleicao, "cargo": cargo, "num": numero, "ue": ue.upper() if ue else None,
                       "num2": numero2, "ue2": ue2.upper() if ue2 else None}


@router.get("/ufs")
def mapa_ufs(f: Filtros = Depends()):
    fonte = """SELECT sg_uf AS area, nr_votavel, sg_ue, qt_votos
                 FROM tse.votos_uf WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s"""
    comando = f"WITH {_indicadores(fonte)}" + _feature_collection(
        "ST_AsGeoJSON(g.geom, 4)",
        f"'id', g.sg_uf, 'nome', g.sg_uf, {PROPS_INDICADOR}",
        "ind i JOIN geo.uf g ON g.sg_uf = i.area",
    )
    return _geojson(consultar_valor(comando, f.params))


@router.get("/municipios")
def mapa_municipios(uf: str = Query(pattern=UF_REGEX), f: Filtros = Depends()):
    fonte = """SELECT cd_municipio AS area, nr_votavel, sg_ue, qt_votos
                 FROM tse.votos_municipio
                WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s AND sg_uf = %(uf)s"""
    comando = f"WITH {_indicadores(fonte)}" + _feature_collection(
        "ST_AsGeoJSON(ST_SimplifyPreserveTopology(g.geom, 0.0005), 5)",
        f"'id', m.cd_municipio, 'nome', m.nm_municipio, {PROPS_INDICADOR}",
        """ind i JOIN tse.municipio m ON m.cd_municipio = i.area
                 JOIN geo.municipio g ON g.cd_ibge = m.cd_ibge""",
    )
    return _geojson(consultar_valor(comando, {**f.params, "uf": uf.upper()}))


@router.get("/bairros")
def mapa_bairros(municipio: int, f: Filtros = Depends()):
    """Bairros oficiais do IBGE (polígonos) e, onde não há, bairros informados pelo TSE (ponto no centro dos locais)."""
    fonte = """SELECT l.bairro_chave AS area, v.nr_votavel, v.sg_ue, sum(v.qt_votos) AS qt_votos
                 FROM tse.votos_local v
                 JOIN tse.local_votacao l
                   ON l.cd_municipio = v.cd_municipio AND l.nr_zona = v.nr_zona AND l.nr_local_votacao = v.nr_local_votacao
                WHERE v.cd_eleicao = %(ele)s AND v.cd_cargo = %(cargo)s AND v.cd_municipio = %(mun)s
                  AND l.bairro_chave IS NOT NULL
                GROUP BY l.bairro_chave, v.nr_votavel, v.sg_ue"""
    geometrias = """,
        g AS (
            SELECT l.bairro_chave AS area, max(l.bairro_nome) AS nome, max(l.bairro_fonte) AS fonte,
                   count(*) AS qt_locais, max(l.cd_bairro_ibge) AS cd_bairro,
                   ST_Centroid(ST_Collect(l.geom) FILTER (WHERE l.coord_valida)) AS centro
              FROM tse.local_votacao l
             WHERE l.cd_municipio = %(mun)s AND l.bairro_chave IS NOT NULL
             GROUP BY l.bairro_chave
        )"""
    comando = f"WITH {_indicadores(fonte)}{geometrias}" + _feature_collection(
        "ST_AsGeoJSON(coalesce(ST_SimplifyPreserveTopology(b.geom, 0.00005), g.centro), 6)",
        f"'id', g.area, 'nome', g.nome, 'fonte', g.fonte, 'qt_locais', g.qt_locais, {PROPS_INDICADOR}",
        "ind i JOIN g ON g.area = i.area LEFT JOIN geo.bairro b ON b.cd_bairro = g.cd_bairro",
    )
    return _geojson(consultar_valor(comando, {**f.params, "mun": municipio}))


@router.get("/locais")
def mapa_locais(municipio: int, f: Filtros = Depends()):
    """Um ponto por local de votação, com indicadores, seções e comparecimento.
    Locais sem coordenada ou com coordenada inconsistente (fora do município) vêm com geometry null."""
    fonte = """SELECT nr_zona || '-' || nr_local_votacao AS area, nr_votavel, sg_ue, qt_votos
                 FROM tse.votos_local
                WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s AND cd_municipio = %(mun)s"""
    comparecimento = """,
        c AS (
            SELECT nr_zona || '-' || nr_local_votacao AS area, count(*) AS qt_secoes,
                   sum(qt_aptos) AS qt_aptos, sum(qt_comparecimento) AS qt_comparecimento,
                   sum(qt_abstencoes) AS qt_abstencoes
              FROM tse.comparecimento_secao
             WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s AND cd_municipio = %(mun)s
             GROUP BY nr_zona, nr_local_votacao
        )"""
    comando = f"WITH {_indicadores(fonte)}{comparecimento}" + _feature_collection(
        "CASE WHEN l.coord_valida THEN ST_AsGeoJSON(l.geom, 6) END",
        f"""'id', i.area, 'zona', l.nr_zona,
            'coord', CASE WHEN l.coord_valida THEN 'ok' WHEN l.geom IS NULL THEN 'ausente' ELSE 'inconsistente' END,
            'local', l.nr_local_votacao, 'nome', l.nm_local_votacao,
            'endereco', l.ds_endereco, 'bairro', l.bairro_nome, 'qt_secoes', c.qt_secoes, 'qt_aptos', c.qt_aptos,
            'qt_comparecimento', c.qt_comparecimento, 'qt_abstencoes', c.qt_abstencoes, {PROPS_INDICADOR}""",
        """ind i
           JOIN (SELECT nr_zona || '-' || nr_local_votacao AS area, nr_zona, nr_local_votacao, nm_local_votacao,
                        ds_endereco, bairro_nome, coord_valida, geom
                   FROM tse.local_votacao WHERE cd_municipio = %(mun)s) l ON l.area = i.area
           LEFT JOIN c ON c.area = i.area""",
    )
    return _geojson(consultar_valor(comando, {**f.params, "mun": municipio}))


@router.get("/municipio")
def contorno_municipio(municipio: int):
    """Contorno simplificado e dados básicos de um município (para links diretos e busca)."""
    dado = consultar_valor(
        """SELECT json_build_object(
                      'id', m.cd_municipio, 'nome', m.nm_municipio, 'uf', m.sg_uf,
                      'geometry', ST_AsGeoJSON(ST_SimplifyPreserveTopology(g.geom, 0.0005), 5)::json,
                      'bbox', CASE WHEN g.geom IS NOT NULL THEN
                              json_build_array(ST_XMin(g.geom), ST_YMin(g.geom), ST_XMax(g.geom), ST_YMax(g.geom)) END)::text
             FROM tse.municipio m
             LEFT JOIN geo.municipio g ON g.cd_ibge = m.cd_ibge
            WHERE m.cd_municipio = %(mun)s""",
        {"mun": municipio},
    )
    if not dado:
        raise HTTPException(status_code=404, detail="Município não encontrado")
    return _resposta(dado)


@router.get("/foco")
def contorno_foco(nivel: str = Query(pattern="^(brasil|uf)$"), uf: str | None = Query(default=None, pattern=UF_REGEX)):
    """Contorno simplificado do território em foco (Brasil = união das UFs; ou uma UF), usado pela máscara do mapa."""
    if nivel == "uf" and not uf:
        raise HTTPException(status_code=422, detail="Informe a UF")
    if nivel == "brasil":
        comando = """SELECT json_build_object('geometry', ST_AsGeoJSON(
                            ST_SimplifyPreserveTopology(ST_Union(geom), 0.02), 3)::json)::text FROM geo.uf"""
    else:
        comando = """SELECT json_build_object('geometry', ST_AsGeoJSON(
                            ST_SimplifyPreserveTopology(geom, 0.005), 4)::json)::text FROM geo.uf WHERE sg_uf = %(uf)s"""
    dado = consultar_valor(comando, {"uf": uf.upper() if uf else None})
    if not dado:
        raise HTTPException(status_code=404, detail="Território não encontrado")
    return _resposta(dado)


@router.get("/busca")
def buscar_municipio(q: str = Query(min_length=2, max_length=60)):
    """Municípios pelo nome (sem acento; nomes que começam com o termo primeiro)."""
    return _resposta(consultar(
        """SELECT cd_municipio AS id, nm_municipio AS nome, sg_uf AS uf
             FROM tse.municipio
            WHERE sg_uf <> 'ZZ' AND unaccent(nm_municipio) ILIKE '%%' || unaccent(%(q)s) || '%%'
            ORDER BY (unaccent(nm_municipio) ILIKE unaccent(%(q)s) || '%%') DESC, length(nm_municipio), nm_municipio
            LIMIT 10""",
        {"q": q},
    ))


@router.get("/resumo")
def resumo(cd_eleicao: int, cargo: int, uf: str | None = Query(default=None, pattern=UF_REGEX),
           municipio: int | None = None, limite: int = Query(default=12, ge=1, le=100)):
    """Totais do recorte (Brasil, UF ou município): candidatos (ou partidos, para cargos estaduais no nível Brasil),
    válidos, brancos, nulos e comparecimento."""
    p = {"ele": cd_eleicao, "cargo": cargo, "uf": uf.upper() if uf else None, "mun": municipio, "lim": limite}
    por_partido = cargo != 1 and not uf and not municipio
    if municipio:
        fonte = """SELECT sg_ue, nr_votavel, sum(qt_votos)::bigint AS q FROM tse.votos_municipio
                    WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s AND cd_municipio = %(mun)s
                    GROUP BY sg_ue, nr_votavel"""
    else:
        fonte = """SELECT sg_ue, nr_votavel, sum(qt_votos)::bigint AS q FROM tse.votos_uf
                    WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s AND (%(uf)s::text IS NULL OR sg_uf = %(uf)s)
                    GROUP BY sg_ue, nr_votavel"""
    if por_partido:
        lista = """
            SELECT json_agg(x ORDER BY x.votos DESC) FROM (
                SELECT left(v.nr_votavel::text, 2)::int AS nr, NULL::text AS ue,
                       coalesce(max(n.sg_partido), 'Nº ' || left(v.nr_votavel::text, 2)) AS nome,
                       max(n.sg_partido) AS partido, NULL::text AS situacao, sum(v.q) AS votos,
                       round(100.0 * sum(v.q) / NULLIF((SELECT validos FROM tot), 0), 2) AS pct
                  FROM v LEFT JOIN nomes n ON n.sg_ue = v.sg_ue AND n.nr_votavel = v.nr_votavel
                 WHERE v.nr_votavel NOT IN (95, 96)
                 GROUP BY left(v.nr_votavel::text, 2)
                 ORDER BY sum(v.q) DESC LIMIT %(lim)s) x"""
    else:
        lista = f"""
            SELECT json_agg(x ORDER BY x.votos DESC) FROM (
                SELECT v.nr_votavel AS nr, v.sg_ue AS ue, n.nome, n.sg_partido AS partido, n.situacao,
                       v.q AS votos, round(100.0 * v.q / NULLIF((SELECT validos FROM tot), 0), 2) AS pct
                  FROM v LEFT JOIN nomes n ON n.sg_ue = v.sg_ue AND n.nr_votavel = v.nr_votavel
                 WHERE {FILTRO_NOMINAL.replace('nr_votavel', 'v.nr_votavel')}
                 ORDER BY v.q DESC LIMIT %(lim)s) x"""
    comando = f"""
        WITH {NOMES.strip()},
        v AS ({fonte}),
        tot AS (SELECT sum(q) FILTER (WHERE nr_votavel NOT IN (95, 96)) AS validos,
                       sum(q) FILTER (WHERE nr_votavel = 95) AS brancos,
                       sum(q) FILTER (WHERE nr_votavel = 96) AS nulos, sum(q) AS total,
                       count(*) FILTER (WHERE {FILTRO_NOMINAL}) AS qt_candidatos FROM v),
        comp AS (SELECT sum(qt_aptos) FILTER (WHERE st_secao_instalada) AS aptos,
                        sum(qt_comparecimento) AS comparecimento, sum(qt_abstencoes) AS abstencoes,
                        count(*) AS secoes
                   FROM tse.comparecimento_secao
                  WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s
                    AND (%(uf)s::text IS NULL OR sg_uf = %(uf)s)
                    AND (%(mun)s::int IS NULL OR cd_municipio = %(mun)s))
        SELECT json_build_object(
                   'agrupamento', {"'partido'" if por_partido else "'candidato'"},
                   'validos', t.validos, 'brancos', t.brancos, 'nulos', t.nulos, 'total', t.total,
                   'qt_candidatos', t.qt_candidatos,
                   'aptos', c.aptos, 'comparecimento', c.comparecimento, 'abstencoes', c.abstencoes,
                   'secoes', c.secoes,
                   'atualizado_em', (SELECT max(dt_geracao) FROM tse.carga_controle
                                      WHERE status = 'OK' AND arquivo ~ '^[A-Z]{{2}}$'),
                   'itens', ({lista}))::text
          FROM tot t, comp c"""
    return _resposta(consultar_valor(comando, p))


@router.get("/local")
def detalhe_local(cd_eleicao: int, cargo: int, municipio: int, zona: int, local: int):
    """Local de votação com o resultado de cada seção (todos os votáveis, nome de urna) e o comparecimento."""
    info = consultar(
        """SELECT l.nm_local_votacao, l.ds_endereco, l.bairro_nome, l.bairro_fonte, l.nr_cep,
                  m.nm_municipio, m.sg_uf, ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon
             FROM tse.local_votacao l JOIN tse.municipio m ON m.cd_municipio = l.cd_municipio
            WHERE l.cd_municipio = %(mun)s AND l.nr_zona = %(zona)s AND l.nr_local_votacao = %(local)s""",
        {"mun": municipio, "zona": zona, "local": local},
    )
    if not info:
        raise HTTPException(status_code=404, detail="Local de votação não encontrado")
    secoes = consultar(
        f"""WITH {NOMES.strip()}
           SELECT c.nr_secao, c.qt_aptos, c.qt_comparecimento, c.qt_abstencoes, c.qt_votos_brancos, c.qt_votos_nulos,
                  c.st_secao_instalada,
                  (SELECT json_agg(json_build_object('nr', v.nr_votavel, 'ue', v.sg_ue, 'nome', n.nome,
                                                     'partido', n.sg_partido, 'votos', v.qt_votos)
                                   ORDER BY v.qt_votos DESC, v.nr_votavel)
                     FROM tse.votacao_secao v
                     LEFT JOIN nomes n ON n.sg_ue = v.sg_ue AND n.nr_votavel = v.nr_votavel
                    WHERE v.cd_municipio = c.cd_municipio AND v.nr_zona = c.nr_zona AND v.nr_secao = c.nr_secao
                      AND v.cd_eleicao = c.cd_eleicao AND v.cd_cargo = c.cd_cargo) AS votos
             FROM tse.comparecimento_secao c
            WHERE c.cd_eleicao = %(ele)s AND c.cd_cargo = %(cargo)s AND c.cd_municipio = %(mun)s
              AND c.nr_zona = %(zona)s AND c.nr_local_votacao = %(local)s
            ORDER BY c.nr_secao""",
        {"ele": cd_eleicao, "cargo": cargo, "mun": municipio, "zona": zona, "local": local},
    )
    return _resposta({"local": info[0], "secoes": secoes})


@router.get("/candidatos")
def candidatos(cd_eleicao: int, cargo: int, uf: str | None = Query(default=None, pattern=UF_REGEX),
               busca: str | None = Query(default=None, min_length=2, max_length=60),
               limite: int = Query(default=30, ge=1, le=100)):
    """Candidatos do cargo ordenados por votos (no Brasil ou na UF), com nome de urna, partido e % dos válidos."""
    return _resposta(consultar(
        f"""WITH {NOMES.strip()},
           v AS (
               SELECT sg_ue, nr_votavel, sum(qt_votos) AS qt_votos
                 FROM tse.votos_uf
                WHERE cd_eleicao = %(ele)s AND cd_cargo = %(cargo)s
                  AND (%(uf)s::text IS NULL OR sg_uf = %(uf)s)
                GROUP BY sg_ue, nr_votavel
           ),
           validos AS (SELECT sg_ue, sum(qt_votos) FILTER (WHERE nr_votavel NOT IN (95, 96)) AS validos
                         FROM v GROUP BY sg_ue)
           SELECT v.sg_ue, v.nr_votavel, n.nome AS nm_votavel, n.sg_partido, n.situacao, v.qt_votos,
                  round(100.0 * v.qt_votos / NULLIF(va.validos, 0), 2) AS pct
             FROM v
             JOIN validos va ON va.sg_ue = v.sg_ue
             LEFT JOIN nomes n ON n.sg_ue = v.sg_ue AND n.nr_votavel = v.nr_votavel
            WHERE {FILTRO_NOMINAL.replace('nr_votavel', 'v.nr_votavel')}
              AND (%(busca)s::text IS NULL OR unaccent(n.nome) ILIKE '%%' || unaccent(%(busca)s) || '%%'
                   OR v.nr_votavel::text = %(busca)s)
            ORDER BY v.qt_votos DESC
            LIMIT %(lim)s""",
        {"ele": cd_eleicao, "cargo": cargo, "uf": uf.upper() if uf else None, "busca": busca, "lim": limite},
    ))
