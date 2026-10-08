# Corpus histórico: SUB-0001 con evidencia on-chain real de testnet (perfil 1.2.0)

> **Nota del 2026-10-08.** Este perfil admite `stellar-classic-payment@1.0.0` para `stellar-testnet`, pero el adaptador actual produce la evidencia con 1.1.0. Una evaluación nueva se rechaza con diagnóstico (`invaria demo-testnet` imprime `REFUSED: …`), sin reetiquetar ni usar otro mapping. No hay ninguna evaluación grabada de este corpus que reproducir. Se conservan sin cambios el perfil, los crudos y los esperados, que documentan lo evaluado con el mapping que declaraba. La demo actual es `../subscription-testnet-1.4.0`.

Fue la demo actual con política de cuarentena declarada (perfil `fund-subscription-testnet@1.2.0`, `invaria-engine@0.6.0`). Desde el perfil 1.3.0 la demo actual es `../subscription-testnet-1.3.0`. Este corpus sigue dando 4/4 con `invaria-engine@0.7.0`, que conserva su promesa: un movimiento muxed nunca se atribuye.

Comando (offline): `uv run --locked invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.2.0`. Daba 4/4 hasta el 2026-10-08; hoy se rechaza (nota de arriba).

**Relación con los corpus históricos** (`../subscription-testnet-1.1.0`, perfil 1.1.0; `../subscription-testnet`, perfil 1.0.0):
- **Iguales byte a byte:** `raw/*.csv` (sintéticos) e `identity_links.json`.
- **Sin regrabar:** la evidencia on-chain es la misma, las respuestas reales de testnet de `tests/fixtures/stellar` (`demoa-own`).
- **Plan, enlaces de ejecución y resultados esperados:** iguales (`scenarios.json`). Solo cambian `corpus_id`, `notice` y `profile_ref`.
- **Perfil:** igual a 1.1.0 salvo `profile_ref`, `rules_ref`, una frase del `disclaimer` y `quarantine_policy` en el requisito de `stellar-testnet` (`tests/stellar/test_testnet_vertical.py::test_profile_1_2_0_only_declares_the_quarantine_policy`).

**Qué declara `quarantine_policy`**:
- un registro en cuarentena nunca acredita cumplimiento;
- su existencia deja UNKNOWN solo los controles cuya conclusión podría cambiar al resolverlo;
- la cuarentena de un certificado sigue activa hasta que otro del mismo alcance on-chain la sustituye explícitamente;
- la naturaleza y la operación de un registro solo se usan si el snapshot las respalda;
- una parte no aprobada no es por ello ajena.

`quarantined_records_allowed: false` se mantiene y no se reinterpreta.

**Por qué los esperados no cambian.** Ningún escenario contiene `chain_effect` ni certificados con `quarantined_records`, que son lo único que leen las reglas nuevas. El certificado SAC declara ahora que sustituye al de Horizon del mismo rango, y ninguno de los dos lleva registros (`test_why_the_migrated_expectations_do_not_change`). Los casos de cuarentena se prueban con entradas derivadas en `tests/stellar/test_declared_quarantine.py`.

| ID | Enlaces de ejecución | Esperado |
|---|---|---|
| TN-LINKED | T1 → SUB-0001 (aprobado) | MATCH |
| TN-NO-LINK | ninguno | UNKNOWN (`AMBIGUOUS_MATCH`) |
| TN-OVER-LINKED | T1 y T3 (aprobación errónea) | BREAK, delta +1.000 participaciones |
| TN-LINKED-FAILED | solo T2 (fallida) | UNKNOWN (`AMBIGUOUS_MATCH`) |

No representa un fondo real. La institución, el banco y el TA son sintéticos.
