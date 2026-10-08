# Corpus histórico: SUB-0001 con evidencia on-chain real de testnet (perfil 1.1.0)

> **Nota del 2026-10-08.** Este perfil admite `stellar-classic-payment@1.0.0` para `stellar-testnet`, pero el adaptador actual produce la evidencia con 1.1.0. Una evaluación nueva se rechaza con diagnóstico (`invaria demo-testnet` imprime `REFUSED: …`), sin reetiquetar ni usar otro mapping. No hay ninguna evaluación grabada de este corpus que reproducir. Se conservan sin cambios el perfil, los crudos y los esperados, que documentan lo evaluado con el mapping que declaraba. La demo actual es `../subscription-testnet-1.4.0`.

Fue la demo actual entre los motores 0.2.0 y 0.5.0: perfil `fund-subscription-testnet@1.1.0`, evaluado sucesivamente por `invaria-engine@0.2.0` a `0.5.0`. Desde el perfil 1.2.0 la demo actual es `../subscription-testnet-1.2.0` (perfil 1.2.0, que declara su política de cuarentena). Este corpus se conserva sin cambios; el motor actual 0.6.0 lo evalúa de forma compatible con la promesa de 1.1.0 (un registro en cuarentena que afecta a la operación bloquea toda comparación de entrega), y sigue dando 4/4.

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.1.0`. Daba 4/4 hasta el 2026-10-08; hoy se rechaza (nota de arriba).

**Relación con el corpus histórico** (`../subscription-testnet`, perfil 1.0.0, motor retirado 0.1.0):
- **Iguales byte a byte:** `raw/*.csv` (sintéticos) y `identity_links.json`.
- **Sin regrabar:** la evidencia on-chain es la misma, las respuestas reales de testnet de `tests/fixtures/stellar` (`demoa-own`).
- **Plan, enlaces de ejecución y resultados esperados:** iguales (`scenarios.json`). Solo cambian `corpus_id`, `notice` y `profile_ref`.
- **Perfil:** igual salvo `profile_ref`, `rules_ref`, una frase del `disclaimer` y `quarantine_scope: records_bearing_on_operation` en el requisito de `stellar-testnet`.

**Por qué los esperados no cambian.** Las reglas de 0.2.0 solo leen dos cosas:
- `chain_effect` que afectan a la operación;
- certificados que listan `quarantined_records` (además, la cuarentena acotada exige que el perfil la declare).

Ningún escenario contiene ninguna de las dos (`test_why_the_migrated_expectations_do_not_change`), así que las expectativas escritas antes de ejecutar, se mantienen.

| ID | Enlaces de ejecución | Esperado |
|---|---|---|
| TN-LINKED | T1 → SUB-0001 (aprobado) | MATCH |
| TN-NO-LINK | ninguno | UNKNOWN (`AMBIGUOUS_MATCH`) |
| TN-OVER-LINKED | T1 y T3 (aprobación errónea) | BREAK, delta +1.000 participaciones |
| TN-LINKED-FAILED | solo T2 (fallida) | UNKNOWN (`AMBIGUOUS_MATCH`) |

No representa un fondo real. La institución, el banco y el TA son sintéticos.
