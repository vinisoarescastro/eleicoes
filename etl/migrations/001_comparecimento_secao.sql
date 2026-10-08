-- Comparecimento, abstenção, brancos e nulos por seção e cargo (arquivo detalhe_votacao_secao do TSE).

-- Comporta códigos de carga maiores que UF (ex.: DETALHE)
ALTER TABLE tse.carga_controle ALTER COLUMN arquivo TYPE varchar(10);

CREATE TABLE tse.comparecimento_secao (
    cd_eleicao                integer  NOT NULL,
    sg_uf                     char(2)  NOT NULL,
    sg_ue                     varchar(5) NOT NULL,
    cd_municipio              integer  NOT NULL,
    nr_zona                   integer  NOT NULL,
    nr_secao                  integer  NOT NULL,
    cd_cargo                  smallint NOT NULL,
    qt_aptos                  integer,
    qt_comparecimento         integer,
    qt_abstencoes             integer,
    qt_votos_nominais         integer,
    qt_votos_brancos          integer,
    qt_votos_nulos            integer,
    qt_votos_legenda          integer,
    qt_votos_anulados_apu_sep integer,
    nr_local_votacao          integer,
    dt_recebimento_bu         timestamp,
    dt_prim_tot_parcial       timestamp,
    ds_origem_voto            text,
    st_secao_instalada        boolean,
    st_secao_anulada          boolean,
    cd_modelo_urna            smallint,
    ds_modelo_urna            text
);

CREATE INDEX ix_comparecimento_secao   ON tse.comparecimento_secao (cd_municipio, nr_zona, nr_secao);
CREATE INDEX ix_comparecimento_recorte ON tse.comparecimento_secao (cd_eleicao, cd_cargo, sg_uf, cd_municipio, nr_zona);

GRANT SELECT ON tse.comparecimento_secao TO tse_leitura;
