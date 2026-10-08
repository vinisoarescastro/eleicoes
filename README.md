# dados-tse-26

Carga dos **votos por seção eleitoral** das Eleições 2026 (dados abertos do TSE) em PostgreSQL/PostGIS, com API somente leitura
e **mapa web público** (Brasil → estado → município → bairro → local de votação e seções).

- Fonte: `https://cdn.tse.jus.br/estatistica/sead/odsele/votacao_secao/votacao_secao_<ANO>_<ARQUIVO>.zip`
  - `AC`..`TO`: eleição estadual da UF (Governador, Senador, Deputados)
  - `BR`: Presidente (todas as UFs + exterior)
  - `ZZ`: exterior (publicado vazio em 2026; os votos do exterior estão no `BR`)
- `DETALHE`: `detalhe_votacao_secao/detalhe_votacao_secao_<ANO>.zip`, com aptos, comparecimento, abstenção,
  brancos, nulos e situação da seção, por seção e cargo. Do zip é carregado apenas o `*_BRASIL.csv`
  (os demais CSVs são recortes por UF do mesmo conteúdo).
- `LOCAIS`: `eleitorado_locais_votacao/eleitorado_local_votacao_<ANO>.zip` (endereço, bairro e coordenadas dos locais de votação).
- `CANDIDATOS`: `consulta_cand/consulta_cand_<ANO>.zip` — apenas nome de urna, número, partido/federação e situação.
  CPF, e-mail, título, data de nascimento e demais dados pessoais do arquivo **não são lidos nem gravados**.
- `MALHAS`: contornos de UFs e municípios (API de malhas do IBGE), bairros do Censo 2022 (IBGE) e correspondência de códigos
  de município TSE x IBGE (configuração do painel de resultados do TSE). Não entra no `ALL`: roda quando pedido ou se o banco não tiver malhas.
- Os arquivos são republicados pelo TSE durante a totalização e no 2º turno. O ETL só recarrega quando o arquivo remoto muda (ETag/Last-Modified).

## Estrutura

| Caminho | Função |
|---|---|
| `docker-compose.yml` | Serviços `db` (PostgreSQL 17 + PostGIS), `api` (FastAPI + página do mapa) e `etl` (sob demanda, profile `etl`) |
| `db/init/` | Schema `tse` e usuário somente leitura `tse_leitura` (rodam só com volume vazio) |
| `etl/ingest.py` | Download, validação do layout e carga transacional por arquivo |
| `etl/mapa.py` | Malhas do IBGE, validação de coordenadas, bairros dos locais e agregados do mapa |
| `etl/migrations/` | Alterações de schema posteriores à criação do banco, aplicadas pelo ETL ao iniciar |
| `api/main.py` | Endpoints de consulta de dados |
| `api/mapa.py` | Endpoints públicos do mapa (GeoJSON) |
| `api/static/index.html` | Página do mapa (MapLibre GL + OpenFreeMap, sem chave de API) |
| `api/static/config.json` | Turnos/códigos de eleição e **cores dos candidatos** (destaques fixos + paleta neutra) |

Tabela fato `tse.votacao_secao`, particionada por arquivo de origem; `tse.comparecimento_secao` (detalhe por seção e cargo);
dimensões `eleicao`, `cargo`, `municipio`, `votavel`, `local_votacao`; controle em `tse.carga_controle` e `tse.schema_migracao`.
Mapa: geometrias em `geo.uf`, `geo.municipio`, `geo.bairro`; agregados em `tse.votos_local`, `tse.votos_municipio` e `tse.votos_uf`
(views materializadas, atualizadas pelo ETL quando os votos mudam).

Para mudar o schema, crie `etl/migrations/NNN_descricao.sql` (próximo número). Cada migração roda uma única vez, em transação.
O ETL usa um advisory lock: se outra carga estiver em andamento, a nova execução encerra sem fazer nada.

## Rodando localmente

```bash
cp .env.example .env          # e defina senhas fortes
docker compose up -d db api
docker compose run --rm etl --arquivos AC,BR
```

- Mapa: http://127.0.0.1:8010/
- API: http://127.0.0.1:8010/docs (Swagger)
- Banco: `127.0.0.1:5435`

Outros exemplos:

```bash
docker compose run --rm etl --arquivos ALL            # todas as UFs + BR + ZZ + DETALHE + LOCAIS + CANDIDATOS
docker compose run --rm etl --arquivos MALHAS         # recarrega malhas do IBGE e bairros
docker compose run --rm etl --arquivos DETALHE        # só comparecimento/abstenção
docker compose run --rm etl --arquivos SP --forcar    # recarrega mesmo sem mudança
```

## Endpoints

| Endpoint | Descrição |
|---|---|
| `GET /health` | Verificação do serviço |
| `GET /cargas` | Situação de cada arquivo carregado e data de geração no TSE |
| `GET /eleicoes`, `GET /cargos`, `GET /municipios?uf=SP` | Tabelas auxiliares |
| `GET /secoes?uf=AC&municipio=1120` | Seções do município e local de votação |
| `GET /secoes/votos?uf=AC&municipio=1120&zona=8&secao=72[&cargo=1]` | Votos de uma seção |
| `GET /votavel/secoes?cd_eleicao=6257&cargo=1&numero=13&uf=AC` | Votos de um candidato seção a seção (paginado: `limite`, `deslocamento`) |
| `GET /totais?cd_eleicao=6257&cargo=1[&uf=&municipio=&zona=]` | Totais por votável no recorte |
| `GET /comparecimento?cd_eleicao=6257&cargo=1&agrupar_por=municipio&uf=SP` | Aptos, comparecimento, abstenção, brancos, nulos e percentuais. `agrupar_por`: `total`, `uf`, `municipio`, `zona`, `secao` (paginado) |

Códigos 2026 (1º turno): `6257` federal (Presidente), `6259` estadual. 95 = branco, 96 = nulo.
Cargos: 1 Presidente, 3 Governador, 5 Senador, 6 Dep. Federal, 7 Dep. Estadual, 8 Dep. Distrital.

Regras dos percentuais em `/comparecimento`:
- abstenção = abstenções / aptos das seções **instaladas** (seção não instalada tem comparecimento e abstenção zerados no TSE);
- brancos, nulos e válidos = sobre o **total de votos** do cargo. Para Senador em 2026 (2 vagas), cada eleitor vota duas vezes,
  então o total de votos é ~2× o comparecimento.
Se `API_TOKEN` estiver definido, envie o header `X-API-Key` nos endpoints acima. O mapa e seus endpoints são sempre públicos.

## Mapa

Endpoints públicos (GeoJSON, `Cache-Control: max-age=300`, gzip):

| Endpoint | Descrição |
|---|---|
| `GET /mapa/ufs?cd_eleicao=&cargo=[&numero=&ue=]` | Estados com o mais votado (e % do candidato, se informado) |
| `GET /mapa/municipios?...&uf=SP` | Municípios da UF |
| `GET /mapa/bairros?...&municipio=71072` | Bairros: polígono IBGE onde existe; senão, ponto no centro dos locais do bairro informado pelo TSE |
| `GET /mapa/locais?...&municipio=71072` | Locais de votação (pontos) com zona, seções, aptos e abstenção |
| `GET /mapa/local?cd_eleicao=&cargo=&municipio=&zona=&local=` | Resultado de cada seção do local |
| `GET /mapa/candidatos?cd_eleicao=&cargo=[&uf=&busca=]` | Candidatos por votos (nome de urna, partido, situação) |
| `GET /mapa/resumo?cd_eleicao=&cargo=[&uf=&municipio=]` | Totais do recorte: candidatos (ou partidos, cargos estaduais no Brasil), válidos, brancos, nulos, comparecimento |
| `GET /mapa/busca?q=` | Busca de município por nome |
| `GET /mapa/municipio?municipio=` | Contorno e enquadramento de um município |
| `GET /mapa/foco?nivel=brasil\|uf[&uf=]` | Contorno simplificado do Brasil (união das UFs) ou de uma UF, para a máscara de foco |

Os GeoJSON aceitam `numero`/`ue` e `numero2`/`ue2` (candidatos A e B) e trazem, por área, os 3 mais votados (`top`),
o vencedor, a vantagem sobre o 2º (`margem`, em p.p.) e os % de A e B.

Interface:
- Modos do mapa: **Mais votado** (tom proporcional à vantagem sobre o 2º), **Comparar** (escala divergente entre dois
  candidatos, cinza para disputa de até 5 p.p.) e **% candidato** (rampa de um tom na cor do candidato, 5 faixas por quantis).
- Painel: recorte atual, barras de resultado, participação (comparecimento, abstenção, válidos, brancos/nulos),
  controles do mapa, detalhe do bairro/local e tabela. No celular, o painel vira uma folha inferior.
- Candidatos sempre pelo **nome de urna**. Interface em tons neutros: a cor é usada apenas para os dados.
- Desktop/tablet: o mapa ocupa toda a área e o painel é um **card flutuante** (margens, cantos arredondados, sombra),
  que pode ser recolhido. Celular: folha inferior.
- **Foco geográfico**: fora do território em foco (Brasil, UF ou município), o mapa é escurecido e levemente desfocado
  por uma máscara recortada pelo contorno real (`clip-path` com o polígono projetado a cada quadro). O território em foco
  fica nítido e contornado. Sem desfoque no celular (desempenho) e sem animações com `prefers-reduced-motion`.
  Navegadores sem suporte a `clip-path: path()` mostram apenas o contorno.
- **Tema claro/escuro**: seletor no topo (Sistema, Claro, Escuro). Começa pelo tema do sistema operacional e a escolha
  fica salva no navegador (`localStorage`). Cada tema tem sua paleta de dados validada; Lula permanece vermelho e Flávio
  Bolsonaro azul (tom mais claro no escuro, para contraste).
- **Mapa / Satélite**: seletor sobre o mapa. Convencional segue o tema (OpenFreeMap `positron`/`dark`); satélite usa imagens
  com limites e rótulos sobrepostos. A troca mantém câmera, território em foco, filtros e dados, sem recarregar a página.
  Preferência salva no navegador. Fonte das imagens configurável em `config.json` (`satelite`); a padrão é a Esri World
  Imagery, cujos termos de uso devem ser verificados antes da publicação.
- Transições: a câmera se move no clique (em paralelo com a carga), enquadrando o território no espaço livre ao lado
  do painel. `Esc` volta um nível.

Regras:
- **Mais votado** exclui brancos/nulos (e, para Deputados, votos de legenda). Percentuais sobre **votos válidos**.
- **Cores** (`api/static/config.json`): Lula em vermelho e Flávio Bolsonaro em azul em todos os elementos; vermelho e azul
  são exclusivos desses dois. Demais candidatos: amarelo, verde-água e violeta, por ordem de votos, no máximo 3 cores por
  recorte; os demais em cinza ("Outros"). Paletas validadas para daltonismo (todos os pares). O mapa base é mantido claro
  também no modo escuro, porque a paleta não passa na validação sobre fundo escuro.
- Cargos estaduais no nível Brasil: a cor indica o **partido** do mais votado em cada estado.
- **Bairro**: polígono oficial do IBGE (Censo 2022, 895 municípios) que contém o local; fora disso, nome de bairro do TSE.
- **Coordenadas**: locais sem coordenada ou a mais de 2 km do próprio município (`ETL_LIMITE_COORD_KM`) não são plotados;
  aparecem listados no painel. Os votos deles continuam nos totais.
- O mapa mostra onde as pessoas **votam**, não onde moram. Zonas e seções não têm limite oficial: são representadas pelos locais.

## Deploy na VPS

1. Instale Docker + Compose plugin, copie o projeto (sem o `.env` local) e crie um `.env` novo com senhas próprias.
   Defina `API_TOKEN` para restringir os endpoints de dados brutos; o mapa continua público.
2. Ajuste `PG_SHARED_BUFFERS` (~25% da RAM) e confira o disco. Referência medida no 1º turno de 2026 (carga `ALL`):
   ~68 milhões de linhas de votos + 2,5 milhões de detalhe + locais, malhas e agregados do mapa: banco com ~10 GB.
   Primeira carga: ~45 min de votos + ~6 min de locais/malhas/agregados (SP sozinho: 17 mi linhas, ~14 min).
   Atualização completa após republicação do TSE: ~55 min (inclui ~13 min de atualização dos agregados sem bloquear o mapa).
   Reserve ao menos 30 GB livres: a staging temporária de SP, o WAL, a atualização dos agregados e o 2º turno somam espaço extra.
3. `docker compose up -d --build db api`
4. As portas ficam em `127.0.0.1`. Publique o mapa via proxy reverso com HTTPS, por exemplo com Caddy
   (certificado automático; o servidor deve ter o DNS apontado e as portas 80/443 liberadas):
   ```
   mapa.seudominio.com.br {
       encode gzip
       header {
           Strict-Transport-Security "max-age=31536000"
           X-Content-Type-Options "nosniff"
           Referrer-Policy "strict-origin-when-cross-origin"
           X-Frame-Options "DENY"
       }
       reverse_proxy 127.0.0.1:8010
   }
   ```
   Como o mapa é público, considere limitar requisições por IP (ex.: Nginx `limit_req` ou plugin de rate limit do Caddy)
   e um cache no proxy para os endpoints `/mapa/*`, que já enviam `Cache-Control: max-age=300`.
   Para acessar o banco, use túnel SSH (`ssh -L 5435:127.0.0.1:5435 usuario@vps`) em vez de abrir a porta.
5. Atualização periódica via cron do host (exemplo: de hora em hora):
   ```
   0 * * * * cd /opt/dados-tse-26 && docker compose run --rm etl --arquivos ALL >> /var/log/dados-tse-etl.log 2>&1
   ```
6. Backup: `docker compose exec db pg_dump -U <usuario> -Fc tse > tse.dump` (os dados podem ser recarregados do TSE, mas o dump evita horas de carga).

## Observações

- Os scripts de `db/init` só rodam na criação do volume. Mudanças de schema depois disso exigem script de migração.
- O ETL usa o usuário administrador do banco; a API usa `tse_leitura` com `default_transaction_read_only` e `statement_timeout`.
- Dados públicos do TSE; não há dados pessoais de eleitores.
