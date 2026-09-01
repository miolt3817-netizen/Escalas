-- ===========================================================================
-- Auditoria por TRIGGER e saldo de equidade DERIVADO.
--
-- Rodar DEPOIS que a aplicação criar as tabelas (SQLAlchemy/Alembic).
--
-- Por que trigger e não hook de ORM: hooks do SQLAlchemy não capturam
-- UPDATE em massa, nem SQL cru, nem alteração feita direto no banco. O
-- requisito da Parte 1 é que NADA escape do registro — logo, o registro tem
-- que viver no banco.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Auditoria
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION registrar_auditoria() RETURNS TRIGGER AS $$
DECLARE
    v_usuario   INTEGER;
    v_motivo    TEXT;
    v_registro  TEXT;
    v_antes     JSONB;
    v_depois    JSONB;
BEGIN
    -- O banco não tem como saber QUEM alterou nem POR QUÊ: a aplicação injeta
    -- esses valores por variável de sessão (api/banco.py).
    BEGIN
        v_usuario := NULLIF(current_setting('app.usuario_id', true), '')::INTEGER;
    EXCEPTION WHEN OTHERS THEN
        v_usuario := NULL;
    END;
    v_motivo := COALESCE(NULLIF(current_setting('app.motivo', true), ''), '');

    IF (TG_OP = 'DELETE') THEN
        v_antes := to_jsonb(OLD);
        v_depois := NULL;
    ELSIF (TG_OP = 'UPDATE') THEN
        v_antes := to_jsonb(OLD);
        v_depois := to_jsonb(NEW);
        IF v_antes = v_depois THEN
            RETURN NEW;  -- nada mudou de fato
        END IF;
    ELSE
        v_antes := NULL;
        v_depois := to_jsonb(NEW);
    END IF;

    -- Nem toda tabela tem coluna `id`: `parametros` usa `chave` como chave
    -- primária. A extração via JSONB devolve NULL em vez de estourar erro
    -- quando o campo não existe, então o trigger serve a qualquer tabela.
    v_registro := COALESCE(
        COALESCE(v_depois, v_antes) ->> 'id',
        COALESCE(v_depois, v_antes) ->> 'chave',
        ''
    );

    INSERT INTO auditoria (entidade, registro_id, operacao, usuario_id,
                           quando, antes, depois, motivo)
    VALUES (TG_TABLE_NAME, v_registro, TG_OP, v_usuario,
            NOW(), v_antes, v_depois, v_motivo);

    IF (TG_OP = 'DELETE') THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'plantoes', 'escalas', 'trocas', 'indisponibilidades',
        'preferencias', 'feriados', 'usuarios', 'parametros', 'excecoes'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_auditoria_%1$s ON %1$s', t);
        EXECUTE format(
            'CREATE TRIGGER trg_auditoria_%1$s
             AFTER INSERT OR UPDATE OR DELETE ON %1$s
             FOR EACH ROW EXECUTE FUNCTION registrar_auditoria()', t);
    END LOOP;
END;
$$;


-- ---------------------------------------------------------------------------
-- 2. Saldo de equidade — VIEW MATERIALIZADA, não tabela mutável
--
-- `plantoes` é a única fonte da verdade. Um contador paralelo atualizado "só
-- na publicação" dessincronizaria com trocas e ajustes feitos depois — e toda
-- a compensação futura passaria a operar sobre dado errado, em silêncio.
--
-- O cálculo é PROPORCIONAL À DISPONIBILIDADE: dias em que o bombeiro estava
-- indisponível não entram como dias elegíveis, para que férias não virem
-- déficit a ser "compensado" com sobrecarga na volta.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS vw_equidade CASCADE;

CREATE MATERIALIZED VIEW vw_equidade AS
WITH dias_publicados AS (
    SELECT p.data,
           p.bombeiro_id,
           p.tipo,
           EXTRACT(DOW FROM p.data)::INT AS dow,
           EXISTS (SELECT 1 FROM feriados f WHERE f.data = p.data) AS eh_feriado
    FROM plantoes p
    JOIN escalas e ON e.id = p.escala_id
    WHERE e.status = 'publicada'
),
categorias AS (
    SELECT data, bombeiro_id, 'total' AS categoria FROM dias_publicados
    UNION ALL
    SELECT data, bombeiro_id, 'branca' FROM dias_publicados
        WHERE tipo = 'branca'
    UNION ALL
    SELECT data, bombeiro_id, 'vermelha' FROM dias_publicados
        WHERE tipo = 'vermelha'
    UNION ALL
    SELECT data, bombeiro_id, 'sabado' FROM dias_publicados WHERE dow = 6
    UNION ALL
    SELECT data, bombeiro_id, 'domingo' FROM dias_publicados WHERE dow = 0
    UNION ALL
    SELECT data, bombeiro_id, 'feriado' FROM dias_publicados WHERE eh_feriado
),
bombeiros AS (
    SELECT id FROM usuarios WHERE papel = 'bombeiro' AND ativo
),
realizado AS (
    SELECT b.id AS bombeiro_id,
           c.categoria,
           COUNT(*) FILTER (WHERE c.bombeiro_id = b.id) AS feitos
    FROM bombeiros b
    CROSS JOIN LATERAL (SELECT DISTINCT categoria FROM categorias) k
    LEFT JOIN categorias c
           ON c.categoria = k.categoria
    GROUP BY b.id, c.categoria
),
elegiveis AS (
    SELECT b.id AS bombeiro_id,
           c.categoria,
           COUNT(DISTINCT c.data) FILTER (
               WHERE NOT EXISTS (
                   SELECT 1 FROM indisponibilidades i
                   WHERE i.bombeiro_id = b.id
                     AND c.data BETWEEN i.inicio AND i.fim
               )
           ) AS dias
    FROM bombeiros b
    CROSS JOIN categorias c
    GROUP BY b.id, c.categoria
),
totais AS (
    SELECT categoria, COUNT(DISTINCT data) AS dias_totais
    FROM categorias GROUP BY categoria
),
soma_elegiveis AS (
    SELECT categoria, SUM(dias) AS soma FROM elegiveis GROUP BY categoria
)
SELECT e.bombeiro_id,
       e.categoria,
       COALESCE(r.feitos, 0)                                    AS realizado,
       CASE WHEN s.soma > 0
            THEN t.dias_totais * e.dias::NUMERIC / s.soma
            ELSE 0 END                                          AS esperado,
       COALESCE(r.feitos, 0) - CASE WHEN s.soma > 0
            THEN t.dias_totais * e.dias::NUMERIC / s.soma
            ELSE 0 END                                          AS saldo
FROM elegiveis e
JOIN totais t          ON t.categoria = e.categoria
JOIN soma_elegiveis s  ON s.categoria = e.categoria
LEFT JOIN realizado r  ON r.bombeiro_id = e.bombeiro_id
                      AND r.categoria = e.categoria;

CREATE UNIQUE INDEX ix_vw_equidade ON vw_equidade (bombeiro_id, categoria);


-- Atualização do saldo a cada mudança em plantão.
--
-- Sem CONCURRENTLY: o Postgres proíbe `REFRESH ... CONCURRENTLY` dentro de uma
-- função, e um trigger sempre roda em transação. O refresh comum toma lock
-- exclusivo, mas nesta escala (um corpo de bombeiros, algumas centenas de
-- plantões por ano) leva milissegundos.
--
-- Se o volume crescer a ponto de o lock incomodar, mover o refresh para fora
-- da transação: a aplicação chama `REFRESH MATERIALIZED VIEW CONCURRENTLY
-- vw_equidade` depois do commit. O índice único abaixo já suporta isso.
CREATE OR REPLACE FUNCTION atualizar_equidade() RETURNS TRIGGER AS $$
BEGIN
    REFRESH MATERIALIZED VIEW vw_equidade;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_equidade_plantoes ON plantoes;
CREATE TRIGGER trg_equidade_plantoes
AFTER INSERT OR UPDATE OR DELETE ON plantoes
FOR EACH STATEMENT EXECUTE FUNCTION atualizar_equidade();


-- ---------------------------------------------------------------------------
-- 3. Integridade: no máximo UMA versão publicada por mês/ano
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS ix_escala_publicada_unica
    ON escalas (ano, mes) WHERE status = 'publicada';
