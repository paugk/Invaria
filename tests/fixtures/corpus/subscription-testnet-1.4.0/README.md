# Corpus histórico: SUB-0001 con evidencia on-chain real de testnet (perfil 1.4.0)

Fue la demo actual con mappings admitidos explícitos: perfil `fund-subscription-testnet@1.4.0`, evaluado por `invaria-engine@0.8.0` y hoy por 0.9.0 sin la regla de ausencia con alcance, que no declara. **Histórico desde esa regla**; la demo actual es `../subscription-testnet-1.5.0`. Sigue dando 4/4.

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.4.0`. Debe dar 4/4.

**Relación con `../subscription-testnet-1.3.0`:**
- **Iguales:** crudos, `identity_links.json`, plan, enlaces de ejecución, esperados y `rules_ref`.
- **Cambia en el perfil:** `profile_ref`, el `disclaimer` (reescrito para no pasar de 2000 caracteres; resume las reglas de 1.3.0, que no cambian) y `additional_mapping_refs` en la fuente `stellar-testnet`. Lo comprueba `tests/stellar/test_testnet_vertical.py::test_profile_1_4_0_only_admits_the_adapter_mappings`.

**Qué declara `additional_mapping_refs`.** Son los once mappings con los que el adaptador escribe los registros de la fuente que no son pagos Classic: movimientos SAC, efectos y legs de path payments, fills del DEX, clawbacks, contrapartes tipadas, claimable balances y movimientos no resueltos (`stellar.adapter.MAPPING_REFS`). Admitirlos no los convierte en evidencia de cumplimiento: un `chain_effect` sigue sin cumplir nada. Una evaluación nueva rechaza la evidencia producida por cualquier otro mapping.

**Por qué los esperados no cambian.** La evidencia de DEMOA solo tiene pagos Classic (mapping admitido ya por 1.3.0) y duplicados SAC corroborados. No hay movimientos muxed, cuyos casos se prueban con entradas derivadas en `tests/engine/test_muxed_identity.py`.

| ID | Enlaces de ejecución | Esperado |
|---|---|---|
| TN-LINKED | T1 → SUB-0001 (aprobado) | MATCH |
| TN-NO-LINK | ninguno | UNKNOWN (`AMBIGUOUS_MATCH`) |
| TN-OVER-LINKED | T1 y T3 (aprobación errónea) | BREAK, delta +1.000 participaciones |
| TN-LINKED-FAILED | solo T2 (fallida) | UNKNOWN (`AMBIGUOUS_MATCH`) |

No representa un fondo real. La institución, el banco y el TA son sintéticos.
