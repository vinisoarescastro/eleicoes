"""API somente leitura sobre os votos por seção eleitoral (dados públicos do TSE)."""
import hmac
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

import mapa
from db import consultar, pool

API_TOKEN = os.getenv("API_TOKEN", "")
UF_REGEX = r"^[A-Za-z]{2}$"

@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    yield
    pool.close()


def autenticar(x_api_key: str | None = Header(default=None)) -> None:
    if API_TOKEN and not (x_api_key and hmac.compare_digest(x_api_key, API_TOKEN)):
        raise HTTPException(status_code=401, detail="X-API-Key inválida ou ausente")


app = FastAPI(title="Dados TSE 2026", version="1.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Endpoints de consulta de dados: exigem X-API-Key quando API_TOKEN está definido.
# O mapa (/mapa/*) e a página web são públicos.
dados = APIRouter(dependencies=[Depends(autenticar)])


@app.get("/health")
def health():
    consultar("SELECT 1 AS ok")
    return {"status": "ok"}


@dados.get("/cargas")
def cargas():
    return consultar(
        """SELECT ano_eleicao, arquivo, status, dt_geracao, linhas, concluido_em, mensagem
             FROM tse.carga_controle ORDER BY ano_eleicao, arquivo"""
    )


@dados.get("/eleicoes")
def eleicoes():
    return consultar(
        """SELECT cd_eleicao, ano_eleicao, nr_turno, ds_eleicao, dt_eleicao, tp_abrangencia
             FROM tse.eleicao ORDER BY ano_eleicao, nr_turno, cd_eleicao"""
    )


@dados.get("/cargos")
def cargos():
    return consultar("SELECT cd_cargo, ds_cargo FROM tse.cargo ORDER BY cd_cargo")


@dados.get("/municipios")
def municipios(uf: str = Query(pattern=UF_REGEX)):
    return consultar(
        "SELECT cd_municipio, nm_municipio FROM tse.municipio WHERE sg_uf = %s ORDER BY nm_municipio",
        (uf.upper(),),
    )


@dados.get("/secoes")
def secoes(uf: str = Query(pattern=UF_REGEX), municipio: int = Query(), zona: int | None = None):
    """Seções existentes no município, com o local de votação."""
    return consultar(
        """SELECT DISTINCT v.nr_zona, v.nr_secao, v.nr_local_votacao, l.nm_local_votacao
             FROM tse.votacao_secao v
             LEFT JOIN tse.local_votacao l
                    ON l.cd_municipio = v.cd_municipio AND l.nr_zona = v.nr_zona
                   AND l.nr_local_votacao = v.nr_local_votacao
            WHERE v.sg_uf = %(uf)s AND v.cd_municipio = %(mun)s
              AND (%(zona)s::int IS NULL OR v.nr_zona = %(zona)s)
            ORDER BY v.nr_zona, v.nr_secao""",
        {"uf": uf.upper(), "mun": municipio, "zona": zona},
    )


@dados.get("/secoes/votos")
def votos_da_secao(
    uf: str = Query(pattern=UF_REGEX),
    municipio: int = Query(),
    zona: int = Query(),
    secao: int = Query(),
    cd_eleicao: int | None = None,
    cargo: int | None = None,
):
    """Votos de todos os votáveis (candidatos, legendas, brancos e nulos) em uma seção."""
    return consultar(
        """SELECT v.cd_eleicao, v.cd_cargo, c.ds_cargo, v.nr_votavel, vt.nm_votavel, v.qt_votos
             FROM tse.votacao_secao v
             JOIN tse.cargo c ON c.cd_cargo = v.cd_cargo
             LEFT JOIN tse.votavel vt
                    ON vt.cd_eleicao = v.cd_eleicao AND vt.sg_ue = v.sg_ue
                   AND vt.cd_cargo = v.cd_cargo AND vt.nr_votavel = v.nr_votavel
            WHERE v.sg_uf = %(uf)s AND v.cd_municipio = %(mun)s
              AND v.nr_zona = %(zona)s AND v.nr_secao = %(secao)s
              AND (%(ele)s::int IS NULL OR v.cd_eleicao = %(ele)s)
              AND (%(cargo)s::smallint IS NULL OR v.cd_cargo = %(cargo)s)
            ORDER BY v.cd_eleicao, v.cd_cargo, v.qt_votos DESC""",
        {"uf": uf.upper(), "mun": municipio, "zona": zona, "secao": secao, "ele": cd_eleicao, "cargo": cargo},
    )


@dados.get("/votavel/secoes")
def votos_por_secao_do_votavel(
    cd_eleicao: int,
    cargo: int,
    numero: int,
    uf: str = Query(pattern=UF_REGEX),
    municipio: int | None = None,
    limite: int = Query(default=500, ge=1, le=5000),
    deslocamento: int = Query(default=0, ge=0),
):
    """Votos de um candidato/legenda em cada seção da UF (paginado)."""
    return consultar(
        """SELECT v.cd_municipio, m.nm_municipio, v.nr_zona, v.nr_secao, v.nr_local_votacao, v.qt_votos
             FROM tse.votacao_secao v
             JOIN tse.municipio m ON m.cd_municipio = v.cd_municipio
            WHERE v.sg_uf = %(uf)s AND v.cd_eleicao = %(ele)s
              AND v.cd_cargo = %(cargo)s AND v.nr_votavel = %(num)s
              AND (%(mun)s::int IS NULL OR v.cd_municipio = %(mun)s)
            ORDER BY v.cd_municipio, v.nr_zona, v.nr_secao
            LIMIT %(lim)s OFFSET %(off)s""",
        {"uf": uf.upper(), "ele": cd_eleicao, "cargo": cargo, "num": numero,
         "mun": municipio, "lim": limite, "off": deslocamento},
    )


# Colunas de agrupamento permitidas (lista fechada: nunca interpolar entrada do usuário no SQL)
AGRUPAMENTOS = {
    "total": [],
    "uf": ["c.sg_uf"],
    "municipio": ["c.sg_uf", "c.cd_municipio", "m.nm_municipio"],
    "zona": ["c.sg_uf", "c.cd_municipio", "m.nm_municipio", "c.nr_zona"],
    "secao": ["c.sg_uf", "c.cd_municipio", "m.nm_municipio", "c.nr_zona", "c.nr_secao", "c.nr_local_votacao"],
}


VOTOS_TOTAL = ("(c.qt_votos_nominais + c.qt_votos_legenda + c.qt_votos_brancos + c.qt_votos_nulos"
               " + coalesce(c.qt_votos_anulados_apu_sep, 0))")


@dados.get("/comparecimento")
def comparecimento(
    cd_eleicao: int,
    cargo: int,
    agrupar_por: str = Query(default="total", pattern="^(total|uf|municipio|zona|secao)$"),
    uf: str | None = Query(default=None, pattern=UF_REGEX),
    municipio: int | None = None,
    zona: int | None = None,
    secao: int | None = None,
    limite: int = Query(default=1000, ge=1, le=5000),
    deslocamento: int = Query(default=0, ge=0),
):
    """Aptos, comparecimento, abstenção, brancos e nulos no recorte, agrupados conforme `agrupar_por`.

    Percentuais:
    - abstenção sobre os aptos das seções instaladas (seção não instalada não gera abstenção no TSE);
    - brancos, nulos e válidos sobre o total de votos do cargo. Em cargos com mais de uma vaga por
      eleitor (Senador em 2026: 2 votos), o total de votos é maior que o comparecimento.
    """
    grupo = AGRUPAMENTOS[agrupar_por]
    colunas = "".join(f"{c}, " for c in grupo)
    group_by = f"GROUP BY {', '.join(grupo)} ORDER BY {', '.join(grupo)}" if grupo else ""
    return consultar(
        f"""SELECT {colunas}
                   count(DISTINCT (c.cd_municipio, c.nr_zona, c.nr_secao)) AS qt_secoes,
                   count(DISTINCT (c.cd_municipio, c.nr_zona, c.nr_secao))
                       FILTER (WHERE NOT c.st_secao_instalada) AS qt_secoes_nao_instaladas,
                   sum(c.qt_aptos) AS qt_aptos,
                   sum(c.qt_comparecimento) AS qt_comparecimento,
                   sum(c.qt_abstencoes) AS qt_abstencoes,
                   sum({VOTOS_TOTAL}) AS qt_votos_total,
                   sum(c.qt_votos_nominais) AS qt_votos_nominais,
                   sum(c.qt_votos_legenda) AS qt_votos_legenda,
                   sum(c.qt_votos_brancos) AS qt_votos_brancos,
                   sum(c.qt_votos_nulos) AS qt_votos_nulos,
                   round(100.0 * sum(c.qt_abstencoes)
                         / NULLIF(sum(c.qt_aptos) FILTER (WHERE c.st_secao_instalada), 0), 2) AS pct_abstencao,
                   round(100.0 * sum(c.qt_votos_brancos) / NULLIF(sum({VOTOS_TOTAL}), 0), 2) AS pct_brancos,
                   round(100.0 * sum(c.qt_votos_nulos) / NULLIF(sum({VOTOS_TOTAL}), 0), 2) AS pct_nulos,
                   round(100.0 * sum(c.qt_votos_nominais + c.qt_votos_legenda)
                         / NULLIF(sum({VOTOS_TOTAL}), 0), 2) AS pct_validos
              FROM tse.comparecimento_secao c
              LEFT JOIN tse.municipio m ON m.cd_municipio = c.cd_municipio
             WHERE c.cd_eleicao = %(ele)s AND c.cd_cargo = %(cargo)s
               AND (%(uf)s::char(2) IS NULL OR c.sg_uf = %(uf)s)
               AND (%(mun)s::int IS NULL OR c.cd_municipio = %(mun)s)
               AND (%(zona)s::int IS NULL OR c.nr_zona = %(zona)s)
               AND (%(secao)s::int IS NULL OR c.nr_secao = %(secao)s)
             {group_by}
             LIMIT %(lim)s OFFSET %(off)s""",
        {"ele": cd_eleicao, "cargo": cargo, "uf": uf.upper() if uf else None, "mun": municipio,
         "zona": zona, "secao": secao, "lim": limite, "off": deslocamento},
    )


@dados.get("/totais")
def totais(
    cd_eleicao: int,
    cargo: int,
    uf: str | None = Query(default=None, pattern=UF_REGEX),
    municipio: int | None = None,
    zona: int | None = None,
):
    """Soma de votos por votável no recorte informado (Brasil, UF, município ou zona)."""
    return consultar(
        """SELECT v.nr_votavel, max(vt.nm_votavel) AS nm_votavel, sum(v.qt_votos) AS qt_votos
             FROM tse.votacao_secao v
             LEFT JOIN tse.votavel vt
                    ON vt.cd_eleicao = v.cd_eleicao AND vt.sg_ue = v.sg_ue
                   AND vt.cd_cargo = v.cd_cargo AND vt.nr_votavel = v.nr_votavel
            WHERE v.cd_eleicao = %(ele)s AND v.cd_cargo = %(cargo)s
              AND (%(uf)s::char(2) IS NULL OR v.sg_uf = %(uf)s)
              AND (%(mun)s::int IS NULL OR v.cd_municipio = %(mun)s)
              AND (%(zona)s::int IS NULL OR v.nr_zona = %(zona)s)
            GROUP BY v.nr_votavel
            ORDER BY qt_votos DESC""",
        {"ele": cd_eleicao, "cargo": cargo, "uf": uf.upper() if uf else None, "mun": municipio, "zona": zona},
    )


app.include_router(dados)
app.include_router(mapa.router)
# Página do mapa (deve ser montada por último para não encobrir as rotas da API)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
