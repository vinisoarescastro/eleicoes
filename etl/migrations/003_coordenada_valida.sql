-- Marca coordenadas do TSE que caem longe do próprio município (cadastro inconsistente).
-- Calculado pelo ETL (mapa.atualizar_bairros); o mapa só plota locais com coord_valida = true.
ALTER TABLE tse.local_votacao ADD COLUMN coord_valida boolean NOT NULL DEFAULT false;

-- Força o recálculo de bairros na próxima execução do ETL
UPDATE tse.local_votacao SET bairro_fonte = NULL;
