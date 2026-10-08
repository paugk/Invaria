# Corpus sintético: suscripción DEMO-A, perfil 1.2.0 (segunda parte)

**Todo en esta carpeta es sintético**, como en `../subscription-synthetic` (ver su README).

Es el corpus actual de suscripción sintética: perfil `fund-subscription-synthetic@1.2.0`, evaluado por `invaria-engine@0.10.0`.

**Relación con `../subscription-synthetic-1.1.0`:**
- **Iguales:** crudos, mappings, observaciones, cobertura e identidades; solo cambia el `corpus_id`.
- **Cambia en el perfil:** `profile_ref`, `rules_ref` (`subscription-synthetic-rules@1.2.0`), una frase del `disclaimer` y `completeness_needs_coverage` en el requisito `token_movement` de `stellar-testnet-frozen`.
- **Esperados:** los mismos, como se registró antes de ejecutar. El certificado on-chain no tiene alcance, así que la igualdad ya era UNKNOWN; las reglas nuevas (alcance coherente de cada representación) no cambian eso.
- Lo comprueba `tests/engine/test_scoped_absence.py::test_subscription_1_2_0_is_1_1_0_with_completeness_and_its_expectations`.
