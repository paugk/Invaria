# Corpus histórico: SUB-0001 con evidencia on-chain real de testnet (perfil 1.5.0)

Fue la demo de la regla de ausencia con alcance: perfil `fund-subscription-testnet@1.5.0`, evaluado por `invaria-engine@0.9.0` y hoy por 0.10.0 sin las reglas de la segunda parte. **Histórico**; la demo actual es `../subscription-testnet-1.6.0`. Sigue dando 4/4.

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.5.0`. Debe dar 4/4.

**Relación con `../subscription-testnet-1.4.0`:**
- **Iguales:** crudos, `identity_links.json`, plan, enlaces de ejecución y esperados.
- **Cambia en el perfil:** `profile_ref`, `rules_ref` (`subscription-testnet-rules@1.5.0`), una frase del `disclaimer` y `absence_needs_chain_scope` en el requisito de `stellar-testnet`.
- **Cambia en `scenarios.json`:** `corpus_id`, `profile_ref` y `notice`.
- Lo comprueba `tests/engine/test_scoped_absence.py::test_testnet_1_5_0_is_1_4_0_with_the_rule_and_the_same_expectations`.

**Por qué no cambia ningún esperado.** Los certificados on-chain los produce el adaptador al reproducir las grabaciones. El de la ruta SAC (`rpc_sac_events`, que incluye la de Horizon) declara un alcance suficiente; el de solo Horizon no basta (`test_a_horizon_only_certificate_cannot_show_the_absence`).
- red y activo (DEMOA, emisor `GCGG…`) de la representación;
- `not_covered` declarado (`claimable_balance_clawback`, `claimable_balance_claim`), que no puede añadir una entrega a la cuenta;
- la cuenta del inversor aprobada (`GC6X…`).

Sin ese alcance, TN-LINKED sería UNKNOWN (`test_real_adapter_certificates_have_a_sufficient_scope`). TN-OVER-LINKED conserva su BREAK con cualquier alcance: entregas no vistas solo aumentarían el exceso.

Los archivos institucionales son sintéticos; la evidencia on-chain es real de testnet (`tests/fixtures/stellar`).
