# Corpus sintético de rescate, perfil 1.6.0 (histórico)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.6.0`; no editar a mano.
Corpus histórico desde la regla de completitud; el actual es `../redemption-synthetic-1.7.0`.
Perfil `fund-redemption-synthetic@1.6.0`, evaluado por `invaria-redemption-engine@0.10.0` y hoy
por 0.11.0 sin las reglas M3, B1 y B3, que no declara. Comparte con los corpus históricos
(`../redemption-synthetic-1.5.0`, `../redemption-synthetic-1.4.0` y
`../redemption-synthetic`) los mismos bytes crudos, observaciones y coberturas. El perfil
añade `absence_needs_chain_scope` al requisito de `stellar-testnet-frozen`: un certificado
on-chain sin alcance declarado suficiente no acredita la ausencia de liquidación.

Los certificados on-chain de este corpus no declaran alcance. Por eso cambian cuatro
esperados, escritos a mano en `V16_EXPECTED` desde la regla:
`no_settlement_after_cancellation` pasa a UNKNOWN (`INSUFFICIENT_COVERAGE`) en RD-CANCELLED,
RD-CANCEL-KNOWN-LATE-AFTER y RD-CANCEL-RETRACTED-BEFORE, que pasan a UNKNOWN, y en
RD-CANCELLED-AFTER-DUE, que conserva el BREAK del pago no hecho. Los demás esperados no
cambian. Los casos de alcance suficiente e insuficiente se prueban en
`tests/engine/test_scoped_absence.py`. Todo es sintético.
