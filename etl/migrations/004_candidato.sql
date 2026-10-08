-- Nome de urna, partido e situação dos candidatos (arquivo consulta_cand do TSE).
-- Somente campos públicos de identificação eleitoral: CPF, e-mail, título, nascimento, gênero, raça,
-- instrução, estado civil e ocupação NÃO são carregados (descartados na leitura pelo ETL).
CREATE TABLE tse.candidato (
    sq_candidato            bigint PRIMARY KEY,
    cd_eleicao              integer  NOT NULL,
    nr_turno                smallint NOT NULL,
    sg_ue                   varchar(5) NOT NULL,
    cd_cargo                smallint NOT NULL,
    nr_candidato            integer  NOT NULL,
    nm_urna                 text,
    nr_partido              smallint,
    sg_partido              varchar(20),
    sg_federacao            varchar(60),
    ds_situacao_candidatura text,
    ds_sit_tot_turno        text
);
CREATE INDEX ix_candidato_numero ON tse.candidato (cd_cargo, sg_ue, nr_candidato);

GRANT SELECT ON tse.candidato TO tse_leitura;
