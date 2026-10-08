-- Executado apenas na primeira inicialização do volume (banco vazio).
CREATE SCHEMA IF NOT EXISTS tse;

CREATE TABLE tse.eleicao (
    cd_eleicao      integer PRIMARY KEY,
    ano_eleicao     smallint NOT NULL,
    nr_turno        smallint NOT NULL,
    cd_tipo_eleicao smallint,
    nm_tipo_eleicao text,
    ds_eleicao      text,
    dt_eleicao      date,
    tp_abrangencia  varchar(2)
);

CREATE TABLE tse.cargo (
    cd_cargo smallint PRIMARY KEY,
    ds_cargo text NOT NULL
);

CREATE TABLE tse.municipio (
    cd_municipio integer PRIMARY KEY,
    sg_uf        char(2) NOT NULL,
    nm_municipio text NOT NULL
);
CREATE INDEX ix_municipio_uf ON tse.municipio (sg_uf, nm_municipio);

-- Candidatos, legendas, brancos (95) e nulos (96) por unidade eleitoral
CREATE TABLE tse.votavel (
    cd_eleicao   integer  NOT NULL,
    sg_ue        varchar(5) NOT NULL,
    cd_cargo     smallint NOT NULL,
    nr_votavel   integer  NOT NULL,
    nm_votavel   text,
    sq_candidato bigint,
    PRIMARY KEY (cd_eleicao, sg_ue, cd_cargo, nr_votavel)
);

CREATE TABLE tse.local_votacao (
    cd_municipio     integer NOT NULL,
    nr_zona          integer NOT NULL,
    nr_local_votacao integer NOT NULL,
    nm_local_votacao text,
    ds_endereco      text,
    PRIMARY KEY (cd_municipio, nr_zona, nr_local_votacao)
);

-- Fato: votos por seção. Particionada pelo arquivo de origem do TSE (AC..TO = eleição estadual da UF,
-- BR = Presidente em todas as UFs e exterior), permitindo recarga isolada (TRUNCATE da partição).
-- As partições são criadas pelo ETL conforme os arquivos carregados.
CREATE TABLE tse.votacao_secao (
    arquivo          varchar(2) NOT NULL,
    cd_eleicao       integer  NOT NULL,
    sg_uf            char(2)  NOT NULL,
    sg_ue            varchar(5) NOT NULL,
    cd_municipio     integer  NOT NULL,
    nr_zona          integer  NOT NULL,
    nr_secao         integer  NOT NULL,
    nr_local_votacao integer,
    cd_cargo         smallint NOT NULL,
    nr_votavel       integer  NOT NULL,
    qt_votos         integer  NOT NULL
) PARTITION BY LIST (arquivo);

CREATE INDEX ix_votacao_secao_secao   ON tse.votacao_secao (cd_municipio, nr_zona, nr_secao);
CREATE INDEX ix_votacao_secao_votavel ON tse.votacao_secao (cd_eleicao, cd_cargo, nr_votavel, sg_uf, cd_municipio);

CREATE TABLE tse.carga_controle (
    ano_eleicao   smallint NOT NULL,
    arquivo       varchar(2) NOT NULL,
    url           text     NOT NULL,
    etag          text,
    last_modified text,
    dt_geracao    timestamp,
    linhas        bigint,
    status        varchar(20) NOT NULL,
    mensagem      text,
    iniciado_em   timestamptz,
    concluido_em  timestamptz,
    PRIMARY KEY (ano_eleicao, arquivo)
);
