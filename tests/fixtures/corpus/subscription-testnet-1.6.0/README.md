# Corpus actual: SUB-0001 con evidencia on-chain real de testnet (perfil 1.6.0)

Es la demo actual (segunda parte): perfil `fund-subscription-testnet@1.6.0`, evaluado por `invaria-engine@0.10.0`.

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.6.0`. Debe dar 4/4.

**Relación con `../subscription-testnet-1.5.0`:**
- **Iguales:** crudos, `identity_links.json`, plan, enlaces y esperados.
- **Cambia en el perfil:** `profile_ref`, `rules_ref` (`subscription-testnet-rules@1.6.0`), una frase del `disclaimer` y `completeness_needs_coverage` en el requisito de `stellar-testnet`.

**Por qué no cambia ningún esperado.** El certificado SAC del adaptador cumple el alcance estricto:
- es coherente con su ruta y su rol: titular, con `not_covered` = `[claimable_balance_clawback, claimable_balance_claim]`, justo lo que esa ruta no observa;
- es de la representación DEMOA de la red de testnet;
- lee la dirección del inversor aprobada en esa red.

Lo comprueban `tests/engine/test_scoped_absence.py::test_b3_the_adapter_certificates_are_coherent_and_the_table_is_current` y `::test_testnet_1_6_0_is_1_5_0_with_completeness_and_the_same_expectations`.

Los archivos institucionales son sintéticos; la evidencia on-chain es real de testnet.
