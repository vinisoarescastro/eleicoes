-- Suporte ao mapa: geometrias (IBGE), localização dos locais de votação (TSE) e agregados de votos.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS geo;

-- Correspondência código TSE x código IBGE do município
ALTER TABLE tse.municipio ADD COLUMN cd_ibge integer;
CREATE INDEX ix_municipio_ibge ON tse.municipio (cd_ibge);

CREATE TABLE geo.uf (
    sg_uf   char(2) PRIMARY KEY,
    cd_ibge smallint NOT NULL,
    geom    geometry(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE geo.municipio (
    cd_ibge integer PRIMARY KEY,
    sg_uf   char(2) NOT NULL,
    geom    geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX ix_geo_municipio_uf ON geo.municipio (sg_uf);

-- Bairros oficiais do Censo 2022 (existem apenas em parte dos municípios)
CREATE TABLE geo.bairro (
    cd_bairro   bigint PRIMARY KEY,
    cd_mun_ibge integer NOT NULL,
    nm_bairro   text NOT NULL,
    geom        geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX ix_geo_bairro_mun  ON geo.bairro (cd_mun_ibge);
CREATE INDEX ix_geo_bairro_geom ON geo.bairro USING gist (geom);

-- Localização e bairro de cada local de votação
ALTER TABLE tse.local_votacao
    ADD COLUMN nm_bairro_tse  text,
    ADD COLUMN nr_cep         varchar(8),
    ADD COLUMN geom           geometry(Point, 4326),
    ADD COLUMN cd_bairro_ibge bigint,
    ADD COLUMN bairro_chave   text,      -- 'I<cd_bairro>' (IBGE) ou 'T<nome normalizado>' (TSE)
    ADD COLUMN bairro_nome    text,
    ADD COLUMN bairro_fonte   varchar(4); -- IBGE | TSE
CREATE INDEX ix_local_votacao_geom ON tse.local_votacao USING gist (geom);

GRANT USAGE ON SCHEMA geo TO tse_leitura;
GRANT SELECT ON ALL TABLES IN SCHEMA geo TO tse_leitura;
ALTER DEFAULT PRIVILEGES IN SCHEMA geo GRANT SELECT ON TABLES TO tse_leitura;

-- Agregados de votos para o mapa (atualizados pelo ETL após cada carga de votos)
CREATE MATERIALIZED VIEW tse.votos_local AS
SELECT cd_eleicao, cd_cargo, sg_uf, sg_ue, cd_municipio, nr_zona,
       coalesce(nr_local_votacao, -1) AS nr_local_votacao, nr_votavel, sum(qt_votos)::integer AS qt_votos
  FROM tse.votacao_secao
 GROUP BY cd_eleicao, cd_cargo, sg_uf, sg_ue, cd_municipio, nr_zona, coalesce(nr_local_votacao, -1), nr_votavel
WITH NO DATA;
CREATE UNIQUE INDEX ux_votos_local ON tse.votos_local (cd_eleicao, cd_cargo, cd_municipio, nr_zona, nr_local_votacao, nr_votavel);
CREATE INDEX ix_votos_local_mun ON tse.votos_local (cd_municipio, cd_eleicao, cd_cargo);

CREATE MATERIALIZED VIEW tse.votos_municipio AS
SELECT cd_eleicao, cd_cargo, sg_uf, sg_ue, cd_municipio, nr_votavel, sum(qt_votos)::integer AS qt_votos
  FROM tse.votos_local
 GROUP BY cd_eleicao, cd_cargo, sg_uf, sg_ue, cd_municipio, nr_votavel
WITH NO DATA;
CREATE UNIQUE INDEX ux_votos_municipio ON tse.votos_municipio (cd_eleicao, cd_cargo, cd_municipio, nr_votavel);
CREATE INDEX ix_votos_municipio_uf ON tse.votos_municipio (cd_eleicao, cd_cargo, sg_uf);

CREATE MATERIALIZED VIEW tse.votos_uf AS
SELECT cd_eleicao, cd_cargo, sg_uf, sg_ue, nr_votavel, sum(qt_votos)::bigint AS qt_votos
  FROM tse.votos_municipio
 GROUP BY cd_eleicao, cd_cargo, sg_uf, sg_ue, nr_votavel
WITH NO DATA;
CREATE UNIQUE INDEX ux_votos_uf ON tse.votos_uf (cd_eleicao, cd_cargo, sg_uf, sg_ue, nr_votavel);

GRANT SELECT ON tse.votos_local, tse.votos_municipio, tse.votos_uf TO tse_leitura;
