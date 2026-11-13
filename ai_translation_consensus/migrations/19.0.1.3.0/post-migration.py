def migrate(cr, version):
    # Every provider that exists before the purposes were introduced translates: give all of them
    # (archived ones too) the purpose "Translations". Providers that already have a purpose keep theirs.
    cr.execute(
        """
        INSERT INTO ai_translation_provider_purpose_rel (provider_id, purpose_id)
        SELECT p.id, d.res_id
          FROM ai_translation_provider p
          JOIN ir_model_data d ON d.module = 'ai_translation_consensus'
                              AND d.name = 'purpose_translate' AND d.model = 'ai.translation.purpose'
         WHERE NOT EXISTS (SELECT 1 FROM ai_translation_provider_purpose_rel r
                            WHERE r.provider_id = p.id)
        ON CONFLICT DO NOTHING
        """
    )
