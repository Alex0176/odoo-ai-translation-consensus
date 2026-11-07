def migrate(cr, version):
    # The Antigravity preset is new; adopt a provider of that name that was created by hand
    # so loading the data file does not violate the unique name constraint.
    cr.execute(
        """
        INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
        SELECT 'ai_translation_consensus', 'provider_antigravity_cli', 'ai.translation.provider', p.id, TRUE
          FROM ai_translation_provider p
         WHERE p.name = 'Antigravity CLI (signed-in)'
           AND NOT EXISTS (SELECT 1 FROM ir_model_data d
                            WHERE d.module = 'ai_translation_consensus'
                              AND d.name = 'provider_antigravity_cli')
         ORDER BY p.id
         LIMIT 1
        """
    )
