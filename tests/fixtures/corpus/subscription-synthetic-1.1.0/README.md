# Corpus sintético: suscripción DEMO-A, perfil 1.1.0

**Todo en esta carpeta es sintético**, como en `../subscription-synthetic` (ver su README, que describe el caso, las fuentes y las derivaciones).

Fue el corpus actual de la regla de ausencia con alcance: perfil `fund-subscription-synthetic@1.1.0`, evaluado por `invaria-engine@0.9.0` y hoy por 0.10.0 sin las reglas de la segunda parte, que no declara. **Histórico**; el actual es `../subscription-synthetic-1.2.0`.

**Relación con `../subscription-synthetic` (perfil 1.0.0):**
- **Iguales byte a byte:** `raw/` y `mappings/`.
- **Iguales salvo `corpus_id`:** observaciones, cobertura e identidades.
- **Cambia en el perfil:** `profile_ref`, `rules_ref` (`subscription-synthetic-rules@1.1.0`), una frase del `disclaimer` y `absence_needs_chain_scope` en el requisito `token_movement` de `stellar-testnet-frozen`.
- **Cambian los esperados**, escritos a mano desde la regla. `subscription.token_units_vs_order` pasa de PASS a UNKNOWN (`INSUFFICIENT_COVERAGE`) en los 8 escenarios donde el certificado on-chain sin alcance la sostenía:
  - K2, K2-historical y DUPLICATE-DELIVERY pasan de MATCH a UNKNOWN;
  - K3 sigue BREAK por la caja (−500,00 USD);
  - los demás ya eran UNKNOWN.
- Lo comprueba `tests/engine/test_scoped_absence.py::test_subscription_1_1_0_is_1_0_0_with_the_rule_and_the_table`.

Se derivó del 1.0.0 con copias y sustituciones textuales (sin regenerar); el 1.0.0 no se tocó.
