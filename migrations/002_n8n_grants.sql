-- Table-level grants for the n8n role created by docker/postgres/init.
-- ${N8N_DB_USER} is substituted by the migration runner from the environment
-- (validated as a bare SQL identifier before substitution).
--
-- n8n gets exactly what the workflows need and nothing more: it confirms
-- delivery (A1), resolves organisations (A7), logs classifier verdicts, and
-- serves the admin forms that edit sources / keywords / settings.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${N8N_DB_USER}') THEN
        RAISE NOTICE 'role ${N8N_DB_USER} not present — skipping grants';
        RETURN;
    END IF;

    GRANT SELECT, UPDATE ON public.seen_items                TO "${N8N_DB_USER}";
    GRANT SELECT, INSERT, UPDATE ON public.org_registry      TO "${N8N_DB_USER}";
    GRANT SELECT, INSERT ON public.items_log                 TO "${N8N_DB_USER}";
    GRANT SELECT, INSERT, UPDATE ON public.sources           TO "${N8N_DB_USER}";
    GRANT SELECT, INSERT, UPDATE ON public.keywords          TO "${N8N_DB_USER}";
    GRANT SELECT, INSERT, UPDATE ON public.settings          TO "${N8N_DB_USER}";
    GRANT SELECT ON public.worker_runs                       TO "${N8N_DB_USER}";
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public    TO "${N8N_DB_USER}";
END
$$;
