# Corpus: SUB-0001 con evidencia on-chain real de testnet

**Mezcla explícita:**
- **Orden, banco y TA:** sintéticos (`raw/*.csv`), importados con los mismos mappings versionados del corpus sintético.
- **Movimiento DEMOA:** real de Stellar testnet, de nuestra emisión propia (ver `tests/fixtures/stellar/README.md` §`demoa-own`). Lo produce en tiempo de ejecución el adaptador Stellar, reproduciendo las respuestas grabadas, sin red.

Esto no representa un fondo real.

## Perfil `fund-subscription-testnet@1.0.0`
Es igual al perfil sintético salvo en tres puntos:
- **Representación:** el emisor real `GCGGXYAK…TBEP` (`DEMOA`, testnet).
- **Fuente on-chain:** `stellar-testnet`, leída por el adaptador.
- **Cobertura mínima de esa fuente:** `provider_claimed` (decisión de la responsable del proyecto, 2026-10-05). Horizon y RPC son ambos de SDF y no hay verificación independiente. Las fuentes institucionales siguen exigiendo `internally_checked`.

## Escenarios (`scenarios.json`: plan + resultados esperados, escritos antes de ejecutar)

| ID | Enlaces de ejecución | Esperado | Por qué |
|---|---|---|---|
| TN-LINKED | T1 → SUB-0001 (aprobado) | MATCH | Única entrega atribuida: T1, 1.000 DEMOA, exitosa. T3 (mismo memo, sin enlace) y T2 (fallida) no cuentan. |
| TN-NO-LINK | ninguno | UNKNOWN (`AMBIGUOUS_MATCH`) | T1 y T3 llegan a la dirección del inversor; ni el memo ni el importe enlazan. |
| TN-OVER-LINKED | T1 y T3 (aprobación errónea) | BREAK, delta +1.000 participaciones | Un enlace equivocado produce una contradicción visible, no un error silencioso. |
| TN-LINKED-FAILED | solo T2 (fallida) | UNKNOWN (`AMBIGUOUS_MATCH`) | Una transacción fallida no tiene efecto, así que no hay entrega enlazada. |

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet`. Debe dar 4/4.
