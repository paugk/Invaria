# Corpus histórico: SUB-0001 con evidencia on-chain real de testnet (perfil 1.3.0)

> **Histórico desde el 2026-10-08.** La demo actual es `../subscription-testnet-1.4.0`: las mismas reglas, más los mappings admitidos para la fuente on-chain. Este corpus sigue dando 4/4 con `invaria-engine@0.8.0`, porque la evidencia de DEMOA solo usa el mapping de pagos Classic, que 1.3.0 admite. Una evidencia de esa fuente con otro mapping, por ejemplo un fill del DEX, se rechazaría.

Fue la demo actual de las cuentas muxed: perfil `fund-subscription-testnet@1.3.0`, evaluado por `invaria-engine@0.7.0`.

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.3.0`. Debe dar 4/4.

**Relación con `../subscription-testnet-1.2.0`** (y, a través de él, con 1.1.0 y 1.0.0):
- **Iguales:** crudos, `identity_links.json`, plan, enlaces de ejecución y esperados.
- **Perfil:** cambian `profile_ref`, `rules_ref`, una frase del `disclaimer`, `correlation.muxed_account_link` y el mapping de `stellar-testnet` (`stellar-classic-payment@1.1.0`), según `tests/stellar/test_testnet_vertical.py::test_profile_1_3_0_only_declares_the_muxed_attribution`.

**Qué declara `muxed_account_link`**:
- un movimiento hacia una sub-cuenta muxed solo se atribuye por un `IdentityLink` (esquema 1.1) que nombre esa dirección M;
- el enlace de su cuenta base no la atribuye;
- uno no atribuido nunca cuenta, pero no es por ello ajeno.

**Memo.** T1 y T3 llevan el memo de texto `SUB-0001`, la orden para la que se enviaron. Se conserva exacto y nunca enlaza: TN-NO-LINK sigue UNKNOWN.

**Por qué los esperados no cambian.** La demo no tiene pagos muxed. Los casos muxed se prueban con entradas derivadas en `tests/engine/test_muxed_identity.py`.

| ID | Enlaces de ejecución | Esperado |
|---|---|---|
| TN-LINKED | T1 → SUB-0001 (aprobado) | MATCH |
| TN-NO-LINK | ninguno | UNKNOWN (`AMBIGUOUS_MATCH`) |
| TN-OVER-LINKED | T1 y T3 (aprobación errónea) | BREAK, delta +1.000 participaciones |
| TN-LINKED-FAILED | solo T2 (fallida) | UNKNOWN (`AMBIGUOUS_MATCH`) |

No representa un fondo real. La institución, el banco y el TA son sintéticos.
